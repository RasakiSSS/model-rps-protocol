import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.cli import make_parser, play
from model_rps.config import RuntimeConfig, load_config
from model_rps.core.diagnostics import LogError, request_trace
from model_rps.core.game import EndReason, GameState
from model_rps.core.logs import RunLogs
from model_rps.players.base import ChatCompletionPlayer, OPENAI, DEEPSEEK, PlayerError, ScriptedPlayer
from model_rps.engine.runner import RequestPolicy, run_game
from model_rps.engine.postmatch import run_postmatch


def decision(move='Paper', summary='PRIVATE-ANALYSIS'):
    return json.dumps(turn_payload([{'type': 'MOVE', 'move': move}], summary), ensure_ascii=False)


def event(delta=None, finish=None, **extra):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}],
                                 **extra}, ensure_ascii=False) + '\r\n\r\n').encode()


DONE = b'data: [DONE]\n\n'


def complete(text, reasoning=None, usage=None):
    result = [event({'role': 'assistant'})]
    if reasoning:
        result += [event({'reasoning_content': reasoning[:3]}), event({'reasoning_content': reasoning[3:]})]
    result += [event({'content': text[:11]}), event({'content': text[11:]}, 'stop')]
    if usage is not None:
        result.append(('data: ' + json.dumps({'choices': [], 'usage': usage}) + '\n\n').encode())
    return result + [DONE]


class Bytes(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, Exception):
                raise part
            if isinstance(part, (float, int)):
                await asyncio.sleep(part)
            elif callable(part):
                part()
            else:
                yield part

    async def aclose(self):
        self.closed = True


def response(parts, status=200):
    return httpx.Response(status, headers={'content-type': 'text/event-stream; charset=utf-8'}, stream=Bytes(parts))


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_reader_closes_before_response_returns_at_done_marker(self):
        from model_rps.llm.streaming import sse_data

        closed = []

        async def tracked(response):
            try:
                async for data in sse_data(response):
                    yield data
            finally:
                closed.append(True)

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(complete(decision())))) as client:
            with patch('model_rps.llm.transport.sse_data', tracked):
                player = ChatCompletionPlayer(client, OPENAI, 'test-key', stream=True)
                await player.respond(GameState().view_for('A'))
                self.assertEqual(closed, [True])

    async def test_postmatch_incomplete_exit_is_never_published_or_kept_in_history(self):
        for recover in (True, False):
            with self.subTest(recover=recover):
                bodies, streams, events = [], [], []
                attempts = {'A': 0, 'B': 0}

                def handler(request):
                    body = json.loads(request.content)
                    bodies.append(body)
                    identity = 'A' if body['model'] == OPENAI.model else 'B'
                    attempts[identity] += 1
                    self.assertNotIn('UNPUBLISHED', json.dumps(body))
                    failed = identity == 'A' and (not recover or attempts[identity] == 1)
                    text = json.dumps(postmatch_payload('UNPUBLISHED' if failed else '已确认的告别'))
                    parts = complete(text, 'NATIVE-PRIVATE')
                    stream = Bytes(parts[:-1] if failed else parts)  # 完整 JSON 和 stop 仍不能替代 DONE。
                    streams.append(stream)
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    players = (ChatCompletionPlayer(client, OPENAI, 'red-key', stream=True),
                               ChatCompletionPlayer(client, DEEPSEEK, 'blue-key', stream=True))
                    result = await run_postmatch(players, {'games': []}, policy=RequestPolicy(1, 0),
                                                 on_event=events.append)
                self.assertEqual(result.complete, recover)
                self.assertEqual(result.exited, {'A': recover, 'B': True})
                self.assertEqual(attempts, {'A': 2 if recover else 3, 'B': 1})
                self.assertNotIn('UNPUBLISHED', repr(events))
                self.assertNotIn('NATIVE-PRIVATE', repr(events))
                self.assertTrue(all(stream.closed for stream in streams))
                a_inputs = [json.loads(b['messages'][-1]['content']) for b in bodies if b['model'] == OPENAI.model]
                for context in a_inputs[1:]:
                    self.assertEqual(context['events'], a_inputs[0]['events'])
                    self.assertEqual(context['exited'], a_inputs[0]['exited'])
                if not recover:
                    self.assertEqual(len(players[0]._messages), 1)

    async def test_split_unicode_crlf_multiline_usage_and_both_adapters(self):
        for spec in (OPENAI, DEEPSEEK):
            with self.subTest(spec=spec.name):
                records = []
                body = decision(summary='中文策略😀')
                native = '私有思考\r\n第二段'
                usage = {'completion_tokens_details': {'reasoning_tokens': 7}}
                wire = b': keepalive\r\n\r\n' + b''.join(complete(body, native, usage))
                # 每个 UTF-8 字节都是一个传输块，含中文和 emoji 的多字节边界。
                wire = wire.replace(b'data: {"choices":', b'data: {\r\ndata: "choices":')
                stream = Bytes([bytes([byte]) for byte in wire])

                def handler(request):
                    payload = json.loads(request.content)
                    self.assertTrue(payload['stream'])
                    self.assertEqual(payload['stream_options'], {'include_usage': True})
                    self.assertIn(spec.token_parameter, payload)
                    self.assertNotIn('authorization', payload)
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    player = ChatCompletionPlayer(client, spec, 'secret', stream=True)
                    with request_trace(records.append, player='A', tick=1, attempt=1):
                        result = await player.respond(GameState().view_for('A'))
                self.assertEqual(result.content, body)
                self.assertTrue(result.has_reasoning)
                self.assertTrue(stream.closed)
                reasoning = next(r for r in records if r['kind'] == 'reasoning')
                self.assertEqual((reasoning['content'], reasoning['reasoning_tokens']), (native, 7))
                self.assertNotIn(native, json.dumps(player._messages, ensure_ascii=False))

    async def test_no_partial_response_is_accepted(self):
        cases = [
            [event({'content': decision()}, 'stop')],  # EOF even with valid JSON and stop
            [event({'content': decision()}), DONE],  # missing stop
            [event({'content': decision()}, 'length'), DONE],
            [event({'content': decision()}, 'content_filter'), DONE],
            [event({'content': decision()}, 'stop'), event({'content': 'more'}), DONE],
            [b'data: {invalid}\n\n'],
            [b'data: {"error":{"message":"SERVER-PRIVATE"}}\n\n'],
            [b'data: {"choices":[]}\n\n'],
            [b'data: {"choices":[{"index":1,"delta":{}}]}\n\n'],
            [event({'content': []}), DONE],
            [event({'tool_calls': [{'type': 'function'}]})],
            [event({'refusal': 'SERVER-PRIVATE'})],
            [event({'content': decision()}, 'stop'), b'data: [DONE]'],
            [event({'content': decision()[:10]}), httpx.ReadError('SERVER-PRIVATE')],
        ]
        for parts in cases:
            with self.subTest(parts=parts):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response(parts))) as client:
                    player = ChatCompletionPlayer(client, OPENAI, 'secret', stream=True)
                    with self.assertRaises(PlayerError) as caught:
                        await player.respond(GameState().view_for('A'))
                    self.assertNotIn('SERVER-PRIVATE', str(caught.exception))
                    self.assertEqual(len(player._messages), 1)

    async def test_http_errors_and_non_stream_reply_do_not_fall_back(self):
        for status in (400, 401, 408, 429, 503):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(status, text='SERVER-PRIVATE')
            )) as client:
                with self.assertRaises(PlayerError) as caught:
                    await ChatCompletionPlayer(client, OPENAI, 'secret', stream=True).respond(GameState().view_for('A'))
                self.assertEqual(caught.exception.retryable, status in (408, 429, 503))
                self.assertNotIn('SERVER-PRIVATE', str(caught.exception))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))) as client:
            with self.assertRaisesRegex(PlayerError, 'SSE'):
                await ChatCompletionPlayer(client, OPENAI, 'secret', stream=True).respond(GameState().view_for('A'))

    async def test_retry_discards_fragments_and_does_not_repeat_successful_peer(self):
        records, inputs, calls = [], [], 0

        def handler(request):
            nonlocal calls
            calls += 1
            inputs.append(json.loads(request.content))
            if calls == 1:
                return response([event({'reasoning_content': 'FAILED-NATIVE', 'content': decision('Rock', 'FAILED-BODY')})])
            # Native reasoning cannot replace ANALYZE, including on a retry.
            if calls == 2:
                return response(complete('{"actions":[{"type":"MOVE","move":"Paper"},{"type":"ENDTURN"}]}'))
            return response(complete(decision('Paper')))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, 'secret', stream=True)
            peer = ScriptedPlayer([decision('Rock')])
            state = await run_game(player, peer, policy=RequestPolicy(2, 0), on_request=records.append)
        self.assertEqual(calls, 3)
        self.assertEqual(state.result.winner, 'A')
        self.assertEqual(len(peer.requests), 1)
        self.assertNotIn('FAILED-NATIVE', json.dumps(inputs))
        self.assertNotIn('FAILED-BODY', json.dumps(inputs))
        valid = [r for r in records if r['kind'] == 'decision' and r['player'] == 'A']
        self.assertEqual([r['attempt'] for r in valid], [3])
        self.assertEqual(len({json.dumps(r['state'], sort_keys=True) for r in records
                              if r['kind'] == 'request_start' and r['player'] == 'A'}), 1)

    async def test_no_action_before_done_and_cancellation_closes_transport(self):
        ready, release = asyncio.Event(), asyncio.Event()
        events, records = [], []

        class Paused(Bytes):
            async def __aiter__(self):
                yield event({'content': decision()}, 'stop')
                ready.set()
                await release.wait()
                yield DONE

        stream = Paused([])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=stream
        ))) as client:
            player = ChatCompletionPlayer(client, OPENAI, 'secret', stream=True)
            task = asyncio.create_task(run_game(player, ScriptedPlayer([decision('Rock')]),
                                               policy=RequestPolicy(3, 0), on_event=events.append,
                                               on_request=records.append))
            await asyncio.wait_for(ready.wait(), 1)
            self.assertFalse(any(e.kind in ('commit', 'reveal') for e in events))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stream.closed)
        self.assertEqual(len(player._messages), 1)
        self.assertTrue(any(r['kind'] == 'stream_end' and r['status'] == 'cancelled' for r in records))
        usage = next(r for r in records if r['kind'] == 'context_usage')
        self.assertIsNone(usage['input_tokens'])
        self.assertEqual(usage['missing_usage_requests'], 1)

    async def test_chat_publishes_only_after_done_and_records_usage_without_consuming_turn(self):
        ready, release, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events, records, contexts = [], [], []
        say = json.dumps({'actions': [{'type': 'SAY', 'text': '独立流式聊天'}]})

        class Paused(Bytes):
            async def __aiter__(self):
                yield event({'content': say}, 'stop')
                ready.set()
                await release.wait()
                yield b'data: {"choices":[],"usage":{"prompt_tokens":100}}\n\n'
                yield DONE

        def handler(request):
            body = json.loads(request.content)
            ctx = json.loads(body['messages'][-1]['content'])
            contexts.append(ctx)
            if len(contexts) == 1:
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Paused([]))
            self.assertTrue(published.is_set())
            return response(complete(decision(), usage={'prompt_tokens': 120}))

        def emit(row):
            events.append(row)
            if row.kind == 'say':
                published.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, 'secret', stream=True)
            task = asyncio.create_task(run_game(player, ScriptedPlayer([decision('Rock')]),
                policy=RequestPolicy(3, 0), on_event=emit, on_request=records.append))
            try:
                await asyncio.wait_for(ready.wait(), 1)
                self.assertFalse(any(e.kind in ('say', 'commit', 'reveal') for e in events))
                release.set()
                state = await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((state.result.winner, state.completed_ticks), ('A', 1))
        self.assertEqual([c['state']['tick'] for c in contexts], [1, 1])
        self.assertEqual(contexts[1]['state']['history'][0]['text'], '独立流式聊天')
        self.assertEqual([e.text for e in events if e.kind == 'say'], ['独立流式聊天'])
        usage = [r for r in records if r['kind'] == 'context_usage']
        self.assertEqual([r['input_tokens'] for r in usage], [100, 120])
        self.assertEqual(usage[-1]['input_tokens_delta'], 20)
        self.assertEqual(usage[-1]['known_input_tokens_total'], 220)

    async def test_first_idle_and_total_timeouts(self):
        for name, parts in (
            ('首包', [0.08, *complete(decision())]),
            ('首包', [b': heartbeat\n\n', 0.08]),
            ('空闲', [event({'role': 'assistant'}), 0.08]),
            ('空闲', [event({'content': 'x'}), b': heartbeat\n\n', 0.08]),
        ):
            stream = Bytes(parts)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
                200, headers={'content-type': 'text/event-stream'}, stream=stream
            ))) as client:
                player = ChatCompletionPlayer(client, OPENAI, 'secret', stream=True,
                                              stream_first_timeout=0.03, stream_idle_timeout=0.03)
                with self.assertRaisesRegex(PlayerError, name):
                    await player.respond(GameState().view_for('A'))
                self.assertTrue(stream.closed)
        records, streams = [], []

        def handler(request):
            stream = Bytes([piece for _ in range(30) for piece in (event({'content': 'x'}), 0.005)])
            streams.append(stream)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            state = await run_game(ChatCompletionPlayer(client, OPENAI, 'secret', stream=True),
                                   ScriptedPlayer([decision('Rock')]), policy=RequestPolicy(0.04, 0),
                                   on_request=records.append)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual(len(streams), 3)
        self.assertTrue(all(s.closed for s in streams))
        self.assertFalse(any(e.kind in ('commit', 'reveal') for e in state.events))

    async def test_live_thought_flush_split_secrets_and_failed_attempt_markers(self):
        with tempfile.TemporaryDirectory() as directory, RunLogs(Path(directory), secrets=('secret-key',)) as logs:
            thought = logs.path / 'red/thought.log'
            seen = []

            def observe():
                text = thought.read_text(encoding='utf-8')
                seen.append(text)
                self.assertIn('第一段', text)
                self.assertIn('第二段', text)
                self.assertNotIn('secret-', text)

            parts = [event({'reasoning_content': t}) for t in ('第一段\r', '\n\n第二段 ', 'secret-', 'key', '\x1b[2J')]
            parts += [observe, event({'content': 'secret-'}), event({'content': 'key'}), httpx.ReadError('failed')]
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response(parts))) as client:
                with request_trace(lambda r: logs.request(1, r), player='A', tick=1, attempt=1):
                    with self.assertRaises(PlayerError):
                        await ChatCompletionPlayer(client, OPENAI, 'secret-key', stream=True).respond(GameState().view_for('A'))
            text = thought.read_text(encoding='utf-8')
            self.assertTrue(seen)
            self.assertIn('    第一段\n    \n    第二段 [REDACTED]\\u001b[2J', text)
            self.assertIn('接收未完成', text)
            self.assertNotIn('\x1b', text)
            debug = (logs.path / 'red/debug.log').read_text(encoding='utf-8')
            self.assertNotIn('secret-key', debug + text)
            self.assertNotIn('secret-', debug)
            self.assertEqual((logs.path / 'chat.log').read_text(encoding='utf-8'), '')
            self.assertEqual((logs.path / 'blue/thought.log').read_text(encoding='utf-8'), '')

    async def test_log_failure_stops_stream_without_retry(self):
        streams = []

        def handler(request):
            stream = Bytes(complete(decision(), 'native-private'))
            streams.append(stream)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

        def fail(record):
            if record['kind'] == 'stream_delta':
                raise LogError('write failed')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(LogError):
                await run_game(ChatCompletionPlayer(client, OPENAI, 'secret', stream=True),
                               ScriptedPlayer([decision('Rock')]), policy=RequestPolicy(2, 0), on_request=fail)
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0].closed)

    async def test_cli_bo_modes_postmatch_history_and_public_privacy(self):
        for initiative, rule in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
            with self.subTest(initiative=initiative, rule=rule), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                keys = root / 'keys.py'
                keys.write_text('OPENAI_API_KEY="g-key"\nDEEPSEEK_API_KEY="d-key"', encoding='utf-8')
                bodies = []

                def handler(request):
                    body = json.loads(request.content)
                    bodies.append(body)
                    self.assertTrue(body['stream'])
                    self.assertNotIn('NATIVE-PRIVATE', json.dumps(body))
                    if json.loads(body["messages"][-1]["content"]).get("phase") == "postmatch":
                        content = json.dumps(postmatch_payload("\n\n".join(['评价', '关键局', '改进'])))
                    elif json.loads(body['messages'][-1]['content'])['phase'] == 'chat':
                        content = '{"actions":[]}'
                    else:
                        content = decision('Paper' if body['model'] == OPENAI.model else 'Rock')
                        context = json.loads(body['messages'][-1]['content'])
                        self.assertNotIn('PRIVATE-ANALYSIS', json.dumps(context.get('state', {})))
                        if context['match']['game'] > 1:
                            self.assertGreater(len(body['messages']), 2)
                    return response(complete(content, 'NATIVE-PRIVATE', {'completion_tokens': 20}))

                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                debug = initiative == 'alternating'
                args = make_parser().parse_args(['--no-config', '--stream', '--bo', '3', '--initiative', initiative,
                                                 '--move-policy', rule, '--secrets-file', str(keys), '--log-dir', str(root / 'logs')]
                                                + (['--debug'] if debug else []))
                output = io.StringIO()
                with patch('model_rps.cli.httpx.AsyncClient', return_value=client), patch('model_rps.cli.open_debug_window'), contextlib.redirect_stdout(output):
                    self.assertEqual(await play(args), 0)
                self.assertEqual(sum(json.loads(body["messages"][-1]["content"]).get("phase") == "postmatch" for body in bodies), 2)
                self.assertNotIn('NATIVE-PRIVATE', output.getvalue())
                self.assertNotIn('接收思考', output.getvalue())
                self.assertNotIn('等待响应', output.getvalue())
                self.assertNotIn('接收正文', output.getvalue())
                self.assertNotIn('PRIVATE-ANALYSIS', output.getvalue().split('赛后公开')[0])
                run, = (root / 'logs').iterdir()
                self.assertNotIn('NATIVE-PRIVATE', (run / 'chat.log').read_text(encoding='utf-8'))
                self.assertEqual(len(list(run.rglob('*.log'))), 5)


class StreamConfigTests(unittest.TestCase):
    def test_defaults_toml_cli_overrides_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[app]\nstream=true\nstream_first_timeout_seconds=18\nstream_idle_timeout_seconds=9', encoding='utf-8')
            parser = make_parser(load_config(path))
            config = RuntimeConfig.from_args(parser.parse_args([]))
            self.assertEqual((config.stream, config.stream_first_timeout_seconds, config.stream_idle_timeout_seconds), (True, 18, 9))
            config = RuntimeConfig.from_args(parser.parse_args(['--no-stream', '--stream-idle-timeout', '4']))
            self.assertEqual((config.stream, config.stream_idle_timeout_seconds), (False, 4))
            self.assertFalse(RuntimeConfig.from_args(make_parser().parse_args([])).stream)
            for source in ('stream="true"', 'stream=1', 'stream_first_timeout_seconds=0',
                           'stream_idle_timeout_seconds=nan', 'stream_idle_timeout_seconds=true'):
                path.write_text('[app]\n' + source, encoding='utf-8')
                with self.subTest(source=source), self.assertRaises(ValueError):
                    load_config(path)
            for args in (['--stream-first-timeout', '-1'], ['--stream-idle-timeout', 'inf']):
                with self.assertRaises(ValueError):
                    RuntimeConfig.from_args(make_parser().parse_args(args))
