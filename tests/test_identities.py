import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from model_rps.cli import make_parser, play
from model_rps.identities import SEAT_DIRECTORIES, named_data, player_names
from model_rps.logs import RunLogs
from model_rps.players import REVIEW_PROMPT


class IdentityTests(unittest.TestCase):
    def test_nested_identity_conversion_preserves_player_text_and_source(self):
        value = {'player': 'A', 'wins': [2, 1], 'thinking': {'A': 'max', 'B': 'high'},
                 'games': [{'result': {'winner': 'B'}, 'events': [{'player': 'B', 'text': 'A/B'}]}],
                 'summary': 'A', 'content': '{"player":"A"}'}
        original = deepcopy(value)
        converted = named_data(value, player_names('model-one', 'model-two'))
        self.assertEqual(value, original)
        self.assertEqual(converted['player'], 'Red · model-one')
        self.assertEqual(converted['wins'], {'Red · model-one': 2, 'Blue · model-two': 1})
        self.assertEqual(converted['games'][0]['result']['winner'], 'Blue · model-two')
        self.assertEqual(converted['games'][0]['events'][0], {'player': 'Blue · model-two', 'text': 'A/B'})
        self.assertEqual(converted['summary'], 'A')
        self.assertEqual(converted['content'], original['content'])

    def test_custom_names_do_not_define_log_paths_or_collide(self):
        names = ['../model', '..\\model', 'CON', 'NUL.txt', '.', 'org/model', 'org_model', 'x' * 200]
        with tempfile.TemporaryDirectory() as directory:
            for name in names:
                with RunLogs(Path(directory), names=player_names(name, name)) as logs:
                    self.assertEqual({p.name for p in logs.path.iterdir()}, {'chat.log', 'red', 'blue'})
        self.assertEqual(player_names('same', 'SAME'), {'A': 'Red · same', 'B': 'Blue · SAME'})


class NamedCLITests(unittest.IsolatedAsyncioTestCase):
    async def test_named_identity_survives_bo_history_review_and_logs_with_custom_models(self):
        for first, second in [('gpt-6-luna', 'deepseek-flash'), ('vendor/gpt', 'vendor/deepseek'), ('same', 'same')]:
            names = player_names(first, second)
            calls = []

            def check(value):
                if isinstance(value, dict):
                    self.assertFalse(set(value) & {'A', 'B'})
                    for key, item in value.items():
                        if key in ('player', 'winner') and item is not None:
                            self.assertIn(item, names.values())
                        check(item)
                elif isinstance(value, list):
                    for item in value:
                        check(item)

            def handler(request):
                slot = 'A' if request.headers['authorization'] == 'Bearer openai-key' else 'B'
                body = json.loads(request.content)
                calls.append(body)
                self.assertEqual(body['model'], first if slot == 'A' else second)
                for message in body['messages']:
                    if message['role'] == 'user':
                        check(json.loads(message['content']))
                ctx = json.loads(body['messages'][-1]['content'])
                if body['messages'][0]['content'] == REVIEW_PROMPT:
                    self.assertEqual(ctx['player'], names[slot])
                    self.assertEqual(ctx['match']['result']['wins'], {names['A']: 2, names['B']: 0})
                    for game in ctx['match']['games']:
                        self.assertEqual(game['result']['winner'], names['A'])
                        self.assertEqual({d['player'] for d in game['decisions']}, set(names.values()))
                    content = {'assessment': '完成', 'key_moments': '第一局', 'improvements': '继续观察'}
                else:
                    self.assertEqual(ctx['state']['player'], names[slot])
                    if ctx['match']['game'] == 2:
                        self.assertEqual(ctx['match']['wins'], {names['A']: 1, names['B']: 0})
                        self.assertEqual(ctx['match']['last_game']['result']['winner'], names['A'])
                        self.assertEqual(len(body['messages']), 4)
                    content = {'decision_summary': 'A/B 是原始文字，不应改写', 'actions': [
                        {'type': 'SAY', 'text': 'A/B'}, {'type': 'MOVE', 'move': 'Paper' if slot == 'A' else 'Rock'}]}
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps(content, ensure_ascii=False)}}]})

            with self.subTest(first=first, second=second), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'keys.py').write_text('OPENAI_API_KEY="openai-key"\nDEEPSEEK_API_KEY="deepseek-key"', encoding='utf-8')
                args = make_parser().parse_args(['--bo', '3', '--openai-model', first, '--deepseek-model', second,
                                                '--secrets-file', str(root / 'keys.py'), '--log-dir', str(root / 'logs')])
                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with patch('model_rps.cli.httpx.AsyncClient', return_value=client), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(await play(args), 0)
                self.assertEqual(len(calls), 6)
                self.assertIn(f"比分 {names['A']} 2 : 0 {names['B']}", output.getvalue())
                self.assertNotIn('Player A', output.getvalue())
                run, = (root / 'logs').iterdir()
                self.assertEqual({p.name for p in run.iterdir()}, {'chat.log', 'red', 'blue'})
                for seat, name in names.items():
                    self.assertIn(f'模型：{name}', (run / SEAT_DIRECTORIES[seat] / 'thought.log').read_text(encoding='utf-8'))
                    for file in (run / SEAT_DIRECTORIES[seat]).glob('debug.log'):
                        for line in file.read_text(encoding='utf-8').splitlines():
                            row = json.loads(line)
                            check(row)
                            if 'player' in row:
                                self.assertEqual(row['player'], name)
                self.assertIn('A/B 是原始文字，不应改写', (run / 'red' / 'thought.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
