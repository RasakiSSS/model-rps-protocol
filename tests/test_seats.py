import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import complete_public_report, postmatch_payload, turn_payload
from model_rps.cli import main, make_parser
from model_rps.config import RuntimeConfig, load_config


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
                        if json.loads(body["messages"][-1]["content"]).get("phase") == "postmatch":
                            content = postmatch_payload("\n\n".join(['完成', '两局', '观察']))
                        elif ctx['phase'] == 'chat':
                            content = {'actions': []}
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
                                content = turn_payload([{'type': 'MOVE', 'move': 'Paper' if seat == 'Red' else 'Rock'}], f'PRIVATE-{seat}')
                        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                            'content': json.dumps(content)}}]})

                    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                    with patch('model_rps.cli.httpx.AsyncClient', return_value=client), contextlib.redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(main(['--config', str(path), '--log-dir', str(root / 'logs')]), 0)
                    turns = 4 if rule == 'revisable' else 2
                    action_and_postmatch = tuple(sum(json.loads(body['messages'][-1]['content'])['phase'] != 'chat'
                                                  for body in calls[seat]) for seat in ('Red', 'Blue'))
                    self.assertEqual(action_and_postmatch, (turns + 2, turns + 1))
                    if mode == 'alternating':
                        self.assertTrue(all(any(json.loads(body['messages'][-1]['content'])['phase'] == 'chat'
                                                for body in calls[seat]) for seat in ('Red', 'Blue')))
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

    def test_independent_chat_cli_bo_postmatch_and_private_usage_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'config.toml'
            config = '[app]\nretry_delay_seconds=0\n[match]\nbest_of=3\ninitiative="alternating"\n'
            for seat in ('red', 'blue'):
                config += (f'[players.{seat}]\nadapter="openai"\nmodel="same-model"\n'
                           'base_url="https://same.test/v1"\napi_key="shared-test-key"\n')
            path.write_text(config, encoding='utf-8')
            action_calls, postmatchs, contexts = {}, [], []

            def handler(request):
                body = json.loads(request.content)
                ctx = json.loads(body['messages'][-1]['content'])
                contexts.append(ctx)
                self.assertNotIn('NATIVE-PRIVATE', json.dumps(body))
                if ctx['phase'] == 'postmatch':
                    postmatchs.append(body)
                    content = postmatch_payload('结合本场聊天复盘。')
                else:
                    state = ctx['state']
                    seat = state['player'].split(' · ')[0]
                    opponent = 'Blue' if seat == 'Red' else 'Red'
                    self.assertNotIn(f'PRIVATE-{opponent}', json.dumps(body))
                    self.assertEqual(ctx['phase'] == 'game', state['can_act'])
                    if state['can_act']:
                        key = (ctx['match']['game'], seat)
                        action_calls[key] = action_calls.get(key, 0) + 1
                        if action_calls[key] == 1:
                            content = {'actions': [{'type': 'SAY', 'text': f'{seat} 先聊天'}]}
                        else:
                            content = turn_payload([{'type': 'MOVE', 'move': 'Paper' if seat == 'Red' else 'Rock'}],
                                                   f'PRIVATE-{seat}')
                    else:
                        content = {'actions': [{'type': 'SAY', 'text': f'{seat} 聊天回复'}]}
                return httpx.Response(200, json={
                    'choices': [{'finish_reason': 'stop', 'message': {
                        'content': json.dumps(content), 'reasoning_content': 'NATIVE-PRIVATE'}}],
                    'usage': {'prompt_tokens': 100 + len(body['messages'])},
                })

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch('model_rps.cli.httpx.AsyncClient', return_value=client), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['--config', str(path), '--log-dir', str(root / 'logs')]), 0)
            self.assertEqual(list(action_calls.values()), [2, 2, 2, 2])
            self.assertTrue(any(c['phase'] == 'chat' for c in contexts))
            self.assertEqual(len(postmatchs), 2)
            self.assertEqual(complete_public_report(postmatchs[0]['messages']),
                             complete_public_report(postmatchs[1]['messages']))
            self.assertNotIn('NATIVE-PRIVATE', out.getvalue())
            self.assertNotIn('PRIVATE-', out.getvalue().split('赛后公开')[0])
            self.assertIn('聊天回复', out.getvalue())
            self.assertIn('Red · same-model 赢得 BO3', out.getvalue())
            run, = (root / 'logs').iterdir()
            for seat in ('red', 'blue'):
                rows = [json.loads(line) for line in (run / seat / 'debug.log').read_text(encoding='utf-8').splitlines()]
                usage = [r for r in rows if r['kind'] == 'context_usage']
                self.assertEqual({r['phase'] for r in usage}, {'game', 'chat', 'postmatch'})
                self.assertTrue(all(r['input_tokens'] > 0 for r in usage))
                self.assertEqual(usage[-1]['known_input_tokens_total'], sum(r['input_tokens'] for r in usage))
                starts = [r for r in rows if r['kind'] == 'request_start' and r['phase'] != 'postmatch']
                self.assertEqual(len(starts), len({(r['game'], r['request_id']) for r in starts}))
                self.assertIn('聊天', (run / seat / 'thought.log').read_text(encoding='utf-8'))
            for file in run.rglob('*.log'):
                self.assertNotIn('shared-test-key', file.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
