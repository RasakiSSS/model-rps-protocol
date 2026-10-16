import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from model_rps.cli import main, make_parser
from model_rps.config import RuntimeConfig, load_config
from model_rps.players import REVIEW_PROMPT


class SeatConfigTests(unittest.TestCase):
    def test_adapter_defaults_and_cli_override_are_independent_of_seat(self):
        config = RuntimeConfig.from_args(make_parser().parse_args([
            '--red-adapter', 'deepseek', '--blue-adapter', 'openai',
            '--red-thinking', 'max', '--blue-thinking', 'medium']))
        self.assertEqual((config.red.adapter, config.blue.adapter), ('deepseek', 'openai'))
        self.assertEqual((config.red.model, config.blue.model), ('deepseek-flash', 'gpt-6-luna'))
        self.assertEqual((config.red.thinking, config.blue.thinking), ('max', 'medium'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[players.red]\nadapter="deepseek"\nmodel="custom"\nthinking="high"', encoding='utf-8')
            config = RuntimeConfig.from_args(make_parser(load_config(path)).parse_args([
                '--red-adapter', 'openai', '--red-thinking', 'medium']))
            self.assertEqual((config.red.model, config.red.adapter, config.red.thinking), ('custom', 'openai', 'medium'))

    def test_invalid_adapter_thinking_and_mixed_old_new_tables_fail_before_requests(self):
        cases = ['[players.red]\nadapter="unknown"', '[players.blue]\nadapter=42',
                 '[players.red]\nadapter="deepseek"\nthinking="medium"',
                 '[players.red]\n[players.deepseek]', '[players.gpt]\n[players.blue]']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for text in cases:
                with self.subTest(text=text):
                    path.write_text(text, encoding='utf-8')
                    with patch('model_rps.cli.httpx.AsyncClient') as client, contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(main(['--config', str(path)]), 2)
                    client.assert_not_called()

    def test_same_model_shared_credentials_isolated_history_and_retry_in_all_modes(self):
        for adapter in ('openai', 'deepseek'):
            for mode, rule in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
                with self.subTest(adapter=adapter, mode=mode, rule=rule), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = root / 'config.toml'
                    text = ('[app]\nretry_delay_seconds=0\n[match]\nbest_of=3\n'
                            f'initiative="{mode}"\nmove_policy="{rule}"\n')
                    for seat in ('red', 'blue'):
                        text += (f'[players.{seat}]\nadapter="{adapter}"\nmodel="same-model"\n'
                                 'base_url="https://same.test/v1"\napi_key="shared-test-key"\n'
                                 f'thinking="{"high" if seat == "red" else "off"}"\n')
                    path.write_text(text, encoding='utf-8')
                    calls = {'Red': [], 'Blue': []}
                    failed = False

                    def handler(request):
                        nonlocal failed
                        body = json.loads(request.content)
                        ctx = json.loads(body['messages'][-1]['content'])
                        name = ctx.get('player', ctx.get('state', {}).get('player'))
                        seat = name.split(' · ')[0]
                        calls[seat].append(body)
                        self.assertEqual(body['model'], 'same-model')
                        self.assertEqual(str(request.url), 'https://same.test/v1/chat/completions')
                        self.assertEqual(request.headers['authorization'], 'Bearer shared-test-key')
                        self.assertIn('max_completion_tokens' if adapter == 'openai' else 'max_tokens', body)
                        self.assertNotIn('max_tokens' if adapter == 'openai' else 'max_completion_tokens', body)
                        expected = 'high' if seat == 'Red' else ('none' if adapter == 'openai' else None)
                        self.assertEqual(body.get('reasoning_effort'), expected)
                        if adapter == 'deepseek':
                            self.assertEqual(body['thinking']['type'], 'enabled' if seat == 'Red' else 'disabled')
                        else:
                            self.assertNotIn('thinking', body)
                        if body['messages'][0]['content'] == REVIEW_PROMPT:
                            content = {'assessment': '完成', 'key_moments': '两局', 'improvements': '观察'}
                        else:
                            opponent = 'Blue' if seat == 'Red' else 'Red'
                            self.assertNotIn(f'PRIVATE-{opponent}', json.dumps(body))
                            self.assertEqual(ctx['state']['player'], f'{seat} · same-model')
                            self.assertNotIn('opponent_move', ctx['state'])
                            if ctx['match']['game'] == 2:
                                self.assertIn(f'PRIVATE-{seat}', json.dumps(body))
                            if seat == 'Red' and not failed:
                                failed = True
                                content = {'invalid': 'PRIVATE-Red-invalid'}
                            else:
                                content = {'decision_summary': f'PRIVATE-{seat}', 'actions': [
                                    {'type': 'MOVE', 'move': 'Paper' if seat == 'Red' else 'Rock'}]}
                        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                            'content': json.dumps(content)}}]})

                    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                    with patch('model_rps.cli.httpx.AsyncClient', return_value=client), contextlib.redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(main(['--config', str(path), '--log-dir', str(root / 'logs')]), 0)
                    turns = 4 if rule == 'revisable' else 2
                    self.assertEqual((len(calls['Red']), len(calls['Blue'])), (turns + 2, turns + 1))
                    self.assertIn('Red · same-model 赢得 BO3', out.getvalue())
                    run, = (root / 'logs').iterdir()
                    self.assertEqual({p.name for p in run.iterdir()}, {'chat.log', 'red', 'blue'})
                    for seat in ('red', 'blue'):
                        rows = [json.loads(line) for line in (run / seat / 'debug.log').read_text(encoding='utf-8').splitlines()]
                        self.assertTrue(all(row['player'] == f'{seat.title()} · same-model' for row in rows if 'player' in row))
                    public_before_end = (run / 'chat.log').read_text(encoding='utf-8').split('赛后公开')[0]
                    self.assertNotIn('PRIVATE-', public_before_end)
                    for file in run.rglob('*.log'):
                        self.assertNotIn('shared-test-key', file.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
