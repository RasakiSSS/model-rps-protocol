import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.cli import main, make_parser
from model_rps.config import RuntimeConfig, load_config
from model_rps.human import HumanPlayer
from model_rps.player_factory import model_players, player_keys


class ProfileConfigTests(unittest.TestCase):
    def parser(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text(text, encoding='utf-8')
            return make_parser(load_config(path))

    def test_human_profile_either_seat_factory_credentials_and_cli_overrides(self):
        parser = self.parser('''[app]
stream=true
[models.bot]
adapter="glm"
api_key="only-model-key"
[profiles.me]
type="human"
name="小明"
[players.red]
profile="bot"
[players.blue]
profile="bot"
''')
        for index, seat in enumerate(('red', 'blue')):
            with self.subTest(seat=seat):
                settings = RuntimeConfig.from_args(parser.parse_args([f'--{seat}-profile', 'me']))
                human = settings.seats[index]
                self.assertEqual((human.type, human.display_name, human.analysis), ('human', '小明', 'optional'))
                self.assertFalse(settings.streaming_for(human)['stream'])
                keys = player_keys(settings, Path('missing.py'))
                self.assertIsNone(keys[index])
                self.assertEqual(keys[1-index], 'only-model-key')
                players = model_players(object(), settings, keys, {'A': 'Red', 'B': 'Blue'})
                self.assertIsInstance(players[index], HumanPlayer)
                self.assertTrue(players[1-index].stream)
                self.assertNotIn('only-model-key', repr(settings))
        selected = RuntimeConfig.from_args(parser.parse_args(
            ['--red-profile', 'me', '--red-name', '用户', '--red-analysis', 'required']))
        self.assertEqual((selected.red.display_name, selected.red.analysis), ('用户', 'required'))
        with self.assertRaisesRegex(ValueError, '一位人类'):
            RuntimeConfig.from_args(parser.parse_args(['--red-profile', 'me', '--blue-profile', 'me']))
        # type 仍是明确的席位覆盖；选择模型条目后可临时改为人类。
        selected = RuntimeConfig.from_args(parser.parse_args(['--red-type', 'human']))
        self.assertEqual(selected.red.type, 'human')
        self.assertEqual(selected.red.analysis, 'optional')

    def test_streaming_inheritance_and_cli_precedence(self):
        parser = self.parser('''[app]
stream=true
stream_first_timeout_seconds=80
stream_idle_timeout_seconds=30
[profiles.wire]
adapter="openai"
api_key="test-key"
stream=true
stream_first_timeout_seconds=70
[profiles.plain]
adapter="openai"
api_key="test-key"
stream=false
stream_first_timeout_seconds=40
stream_idle_timeout_seconds=12
[players.red]
profile="wire"
stream=false
stream_first_timeout_seconds=60
[players.blue]
profile="plain"
''')
        cases = [([], [(False, 60, 30), (False, 40, 12)]),
                 (['--stream'], [(True, 60, 30), (True, 40, 12)]),
                 (['--stream-first-timeout', '55', '--blue-stream-idle-timeout', '4'],
                  [(False, 55, 30), (False, 55, 4)]),
                 (['--no-stream', '--red-stream'], [(True, 60, 30), (False, 40, 12)]),
                 (['--red-stream', '--no-stream'], [(True, 60, 30), (False, 40, 12)]),
                 (['--stream', '--no-blue-stream', '--blue-stream-first-timeout', '9'],
                  [(True, 60, 30), (False, 9, 12)]),
                 (['--red-profile', 'plain'], [(False, 60, 12), (False, 40, 12)])]
        for args, expected in cases:
            with self.subTest(args=args):
                settings = RuntimeConfig.from_args(parser.parse_args(args))
                players = model_players(object(), settings, ('test-key', 'test-key'), {'A': 'Red', 'B': 'Blue'})
                actual = [(p.stream, p.stream_first_timeout, p.stream_idle_timeout) for p in players]
                self.assertEqual(actual, expected)
                self.assertIsNot(players[0]._history, players[1]._history)
        defaults = RuntimeConfig.from_args(make_parser().parse_args([]))
        self.assertEqual(defaults.streaming_for(defaults.red),
                         {'stream': False, 'stream_first_timeout_seconds': 60, 'stream_idle_timeout_seconds': 30})

    def test_invalid_profile_settings_fail_before_network_without_secret_echo(self):
        cases = ['[profiles.me]\ntype="robot"', '[profiles.me]\ntype="human"\nname=""',
                 '[profiles.me]\ntype="human"\nname="A"\n[players.red]\nprofile="me"',
                 '[profiles.me]\ntype="human"\napi_key="test-secret"',
                 '[profiles.me]\ntype="human"\nstream=true',
                 '[profiles.x]\ntype="llm"', '[profiles.x]\nadapter="glm"\nprofile="y"',
                 '[models.x]\nadapter="openai"\n[profiles.x]\ntype="human"',
                 '[profiles.x]\nadapter="glm"\n[profiles.x.nested]\napi_key="test-secret"']
        for header in ('[profiles.x]\nadapter="glm"', '[models.x]\nadapter="glm"', '[players.red]'):
            for invalid in ('stream="false"', 'stream=1', 'stream_first_timeout_seconds=0',
                            'stream_first_timeout_seconds=true', 'stream_idle_timeout_seconds=inf',
                            'stream_idle_timeout_seconds=nan', 'stream_idle_timeout_seconds="30"'):
                cases.append(header + '\n' + invalid)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for content in cases:
                with self.subTest(content=content):
                    path.write_text(content, encoding='utf-8')
                    with patch('model_rps.cli.httpx.AsyncClient') as client, contextlib.redirect_stderr(io.StringIO()) as err:
                        self.assertEqual(main(['--config', str(path), '--stream']), 2)
                    client.assert_not_called()
                    self.assertNotIn('test-secret', err.getvalue())
        for args in (['--red-stream-first-timeout', '-1'], ['--blue-stream-idle-timeout', 'nan']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                RuntimeConfig.from_args(make_parser().parse_args(args))

    def test_template_profiles_support_human_switch_without_type_override(self):
        path = Path(__file__).resolve().parents[1] / 'config.example.toml'
        data = load_config(path)
        # 只给测试中的模型填写假密钥，不修改模板。
        for profile in data['profiles'].values():
            if profile.get('type', 'llm') == 'llm':
                profile['api_key'] = 'template-test-key'
        parser = make_parser(data)
        for seat in ('red', 'blue'):
            settings = RuntimeConfig.from_args(parser.parse_args([f'--{seat}-profile', 'human']))
            self.assertEqual(getattr(settings, seat).type, 'human')


class MixedStreamingCliTests(unittest.TestCase):
    def test_profile_streaming_is_used_for_game_postmatch_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'config.toml'
            path.write_text('''[app]
stream=true
retry_delay_seconds=0
[match]
best_of=3
[profiles.wire]
adapter="openai"
model="wire-test"
api_key="wire-key"
stream_first_timeout_seconds=17
stream_idle_timeout_seconds=11
[profiles.plain]
adapter="anthropic"
model="plain-test"
api_key="plain-key"
stream=false
stream_first_timeout_seconds=29
[players.red]
profile="wire"
[players.blue]
profile="plain"
''', encoding='utf-8')
            calls = []

            def handler(request):
                body = json.loads(request.content)
                wire = body['model'] == 'wire-test'
                ctx = json.loads(body['messages'][-1]['content'])
                calls.append((wire, ctx['phase'], body['stream']))
                self.assertEqual(body['stream'], wire)
                content = json.dumps(postmatch_payload('结束') if ctx['phase'] == 'postmatch' else
                                     turn_payload([{'type': 'MOVE', 'move': 'Paper' if wire else 'Rock'}]))
                if not wire:
                    return httpx.Response(200, json={'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'text', 'text': content}], 'stop_reason': 'end_turn'})
                data = json.dumps({'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': 'stop'}]})
                return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                     content=f'data: {data}\n\ndata: [DONE]\n\n'.encode())

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch('model_rps.cli.httpx.AsyncClient', return_value=client), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['--config', str(path), '--log-dir', str(root / 'logs')]), 0)
            self.assertIn('赢得 BO3', out.getvalue())
            self.assertEqual(calls.count((True, 'game', True)), 2)
            self.assertEqual(calls.count((False, 'game', False)), 2)
            self.assertIn((True, 'postmatch', True), calls)
            self.assertIn((False, 'postmatch', False), calls)
            run, = (root / 'logs').iterdir()
            for seat, enabled, first, idle in (('red', True, 17, 11), ('blue', False, 29, 30)):
                records = [json.loads(line) for line in (run / seat / 'debug.log').read_text(encoding='utf-8').splitlines()]
                start = next(r for r in records if r['kind'] == 'match_start')
                actual = start['player_streaming']['Red · wire-test' if seat == 'red' else 'Blue · plain-test']
                self.assertEqual(actual, {'stream': enabled, 'stream_first_timeout_seconds': first,
                                          'stream_idle_timeout_seconds': idle})
                requests = [r for r in records if r['kind'] == 'request_config']
                self.assertEqual(len(requests), 3)
                self.assertTrue(all((r['stream'], r['stream_first_timeout'], r['stream_idle_timeout']) ==
                                    (enabled, first, idle) for r in requests))
                self.assertNotIn('wire-key', json.dumps(records))
                self.assertNotIn('plain-key', json.dumps(records))


if __name__ == '__main__':
    unittest.main()
