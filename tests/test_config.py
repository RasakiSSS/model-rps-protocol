import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.cli import main, make_parser
from model_rps.config import PlayerConfig, RuntimeConfig, load_config


@contextlib.contextmanager
def workspace():
    with tempfile.TemporaryDirectory() as directory:
        old = Path.cwd()
        os.chdir(directory)
        try:
            yield Path(directory)
        finally:
            os.chdir(old)


def settings(path, options=()):
    return RuntimeConfig.from_args(make_parser(load_config(path, required=True)).parse_args(list(options)))


class ConfigTests(unittest.TestCase):
    def test_precedence_paths_names_and_derived_defaults(self):
        with workspace() as root:
            folder = root / 'profiles'
            folder.mkdir()
            path = folder / 'test.toml'
            path.write_text('[app]\nlog_dir="runs"\ntimeout_seconds=123\nmax_output_tokens=12000\n'
                            '[match]\nbest_of=9\n[players.gpt]\nmodel="gpt-5.6-sol"\nthinking="high"\n', encoding='utf-8')
            config = settings(path, ['--bo', '3', '--timeout', '45', '--openai-thinking', 'off'])
            self.assertEqual((config.best_of, config.timeout_seconds, config.max_output_tokens), (3, 45, 12000))
            self.assertEqual(config.red.model, 'gpt-5.6-sol')
            self.assertEqual(config.red.thinking, 'off')
            self.assertIsNone(config.max_games)
            self.assertEqual(config.blue.thinking, 'off')
            self.assertEqual(config.log_dir, folder / 'runs')
            self.assertEqual(settings(path, ['--log-dir', 'cli-runs']).log_dir, Path('cli-runs'))
            self.assertEqual(settings(path, ['--openai-model', 'second']).red.display_name, 'second')
            self.assertEqual(settings(path, ['--openai-model', 'second', '--openai-name', 'Display']).red.display_name, 'Display')

    def test_unknown_types_ranges_and_future_mode_rejected(self):
        invalid = [
            'unknown=1', '[players]\nother={}', '[players.gpt]\nreasoning="high"',
            'app="live"', '[app]\nmode=1', '[app]\nmode="other"', '[app]\ntimeout_seconds=true',
            '[app]\ntimeout_seconds=nan', '[app]\ntimeout_seconds=inf', '[app]\ntimeout_seconds=0',
            '[app]\ntimeout_seconds=' + '9' * 400,
            '[app]\nretry_delay_seconds=-1', '[app]\nmax_output_tokens=3.5', '[app]\nmax_output_tokens=true',
            '[app]\nlog_dir=[]', '[match]\nbest_of=2', '[match]\nbest_of=true',
            '[match]\nmax_games=0', '[match]\nbest_of=9\nmax_games=2',
            '[match]\ninitiative="async"', '[match]\nmove_policy="other"',
            '[players.gpt]\nmodel=42', '[players.gpt]\nname=""', '[players.gpt]\nname="A"',
            '[players.gpt]\nbase_url="https://secret@example.com/v1"',
            '[players.gpt]\nbase_url="https://@example.com/v1"',
            '[players.gpt]\nbase_url="https://example.com:bad/v1"',
            '[players.deepseek]\nthinking="medium"', '[players.gpt]\napi_key=12',
        ]
        with workspace() as root:
            path = root / 'config.toml'
            for source in invalid:
                with self.subTest(source=source):
                    path.write_text(source, encoding='utf-8')
                    with self.assertRaises(ValueError):
                        settings(path)

    def test_invalid_config_before_network_does_not_echo_credentials(self):
        with workspace() as root:
            path = root / 'config.toml'
            for source in ('[players.gpt]\napi_key="top-secret', '"top-secret"=1',
                           '[players.gpt]\nbase_url="https://top-secret@example.com"'):
                path.write_text(source, encoding='utf-8')
                output = io.StringIO()
                with contextlib.redirect_stderr(output), patch('model_rps.cli.httpx.AsyncClient') as client:
                    self.assertEqual(main([]), 2)
                client.assert_not_called()
                self.assertNotIn('top-secret', output.getvalue())
                self.assertFalse((root / 'logs').exists())
            path.write_bytes(b'\xff')
            with self.assertRaises(ValueError):
                load_config(path)

    def test_optional_default_required_explicit_no_config_and_help(self):
        with workspace() as root:
            self.assertEqual(load_config(), {})
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--config', 'missing.toml', '--mode', 'demo']), 2)
            (root / 'config.toml').write_text('broken = [', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--no-config', '--mode', 'demo']), 0)
                with self.assertRaises(SystemExit) as caught:
                    main(['--help'])
                self.assertEqual(caught.exception.code, 0)

    def test_empty_or_partial_live_keys_do_not_fall_back_to_legacy(self):
        with workspace() as root:
            (root / 'local_secrets.py').write_text('DEEPSEEK_API_KEY="old-d"\nOPENAI_API_KEY="old-g"', encoding='utf-8')
            for source in ('[players.gpt]\napi_key=""', '[players.gpt]\napi_key="new-g"'):
                (root / 'config.toml').write_text(source, encoding='utf-8')
                with contextlib.redirect_stderr(io.StringIO()), patch('model_rps.cli.httpx.AsyncClient') as client:
                    self.assertEqual(main([]), 2)
                client.assert_not_called()
                self.assertFalse((root / 'logs').exists())

    def test_demo_empty_keys_and_bo_override(self):
        with workspace() as root:
            (root / 'config.toml').write_text('[app]\nmode="demo"\n[match]\nbest_of=9\n'
                                            '[players.gpt]\napi_key=""\n[players.deepseek]\napi_key=""', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()), patch('model_rps.cli.httpx.AsyncClient') as client:
                self.assertEqual(main(['--bo', '3']), 0)
            client.assert_not_called()
            run, = (root / 'logs').iterdir()
            start = json.loads((run / 'red/debug.log').read_text(encoding='utf-8').splitlines()[0])
            self.assertEqual((start['best_of'], start['max_games']), (3, 30))

    def test_sensitive_player_config_repr(self):
        self.assertNotIn('hidden-key', repr(PlayerConfig('model', 'https://example.com', 'off', api_key='hidden-key')))

    def test_live_routes_config_to_requests_history_postmatch_and_logs(self):
        for legacy_override in (False, True):
            with self.subTest(legacy_override=legacy_override), workspace() as root:
                (root / 'config.toml').write_text('''[app]
timeout_seconds=7
retry_delay_seconds=0
max_output_tokens=12345
[match]
best_of=3
[players.gpt]
model="custom-gpt"
name="GPT 显示名"
base_url="https://gpt.test/v1"
api_key="new-g"
thinking="high"
[players.deepseek]
model="custom-deepseek"
name="DeepSeek 显示名"
base_url="https://deepseek.test"
api_key="new-d"
thinking="low"
''', encoding='utf-8')
                (root / 'keys.py').write_text('OPENAI_API_KEY="old-g"\nDEEPSEEK_API_KEY="old-d"', encoding='utf-8')
                calls = []

                def handler(request):
                    body = json.loads(request.content)
                    calls.append(body)
                    gpt = body['model'] == 'custom-gpt'
                    key = ('old-' if legacy_override else 'new-') + ('g' if gpt else 'd')
                    self.assertEqual(request.headers['authorization'], 'Bearer ' + key)
                    self.assertEqual(str(request.url), 'https://gpt.test/v1/chat/completions' if gpt else 'https://deepseek.test/chat/completions')
                    self.assertEqual(body.get('max_tokens', body.get('max_completion_tokens')), 12345)
                    self.assertEqual(body['reasoning_effort'], 'high' if gpt else 'low')
                    ctx = json.loads(body['messages'][-1]['content'])
                    self.assertEqual(ctx.get('player', ctx.get('state', {}).get('player')), 'Red · GPT 显示名' if gpt else 'Blue · DeepSeek 显示名')
                    if json.loads(body["messages"][-1]["content"]).get("phase") == "postmatch":
                        self.assertEqual(ctx['match']['result']['wins'], {'Red · GPT 显示名': 2, 'Blue · DeepSeek 显示名': 0})
                        content = postmatch_payload("\n\n".join(['完成', '第二局', key]))
                    else:
                        if ctx['match']['game'] == 2:
                            self.assertEqual(len(body['messages']), 4)
                        content = turn_payload([{'type': 'MOVE', 'move': 'Paper' if gpt else 'Rock'}], key)
                    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}]})

                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with patch('model_rps.cli.httpx.AsyncClient', return_value=client) as factory, contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(['--secrets-file', 'keys.py'] if legacy_override else []), 0)
                factory.assert_called_once_with(timeout=7, follow_redirects=False)
                self.assertEqual(len(calls), 6)
                for file in (root / 'logs').rglob('*.log'):
                    text = file.read_text(encoding='utf-8')
                    for key in ('old-g', 'old-d', 'new-g', 'new-d'):
                        self.assertNotIn(key, text)
                        self.assertNotIn(key, output.getvalue())


if __name__ == '__main__':
    unittest.main()
