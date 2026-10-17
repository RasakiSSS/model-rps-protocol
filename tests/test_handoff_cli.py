import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from fixtures import postmatch_payload, turn_payload
from model_rps.cli import main
from model_rps.console import Console
from model_rps.game import GameState, InvalidResponse
from model_rps.human import HumanPlayer
from model_rps.human_terminal import HumanTerminal
from model_rps.players import ScriptedPlayer
from model_rps.requests import RequestPolicy
from model_rps.scheduler import GameSession
from model_rps.seats import Replacement, SeatPlayer
from test_human import until


class HandoffTerminal:
    def __init__(self, player, identity, console):
        self.player = player

    async def wait_for_resume(self):
        raise AssertionError('模型应直接退出赛后聊天')

    async def run(self, match):
        task = asyncio.create_task(match)
        human = self.player
        try:
            await until(lambda: human._available.is_set())
            for command in ('/handoff unknown', '/handoff human', '/handoff empty'):
                try:
                    await human.submit(command)
                except InvalidResponse:
                    pass
                else:
                    raise AssertionError('无效交接被接受')
            await human.submit('/say OLD-HUMAN-CHAT')
            await until(lambda: human._available.is_set() and human.can_act)
            await human.submit('/move Paper')
            await human.submit('/analyze HUMAN-PRIVATE')
            await human.submit('/endturn')
            # revisable 仍须完成结算 Tick；其他模式等待下一局。
            while human.match.get('game', 1) == 1:
                await until(lambda: human.match.get('game', 1) > 1 or
                            (human.can_act and human._available.is_set()))
                if human.match.get('game', 1) == 1:
                    await human.submit('/endturn')
            await until(lambda: human._available.is_set() and human.can_act)
            await human.submit('/analyze PRIVATE-DRAFT')
            await human.submit('/handoff takeover')
            return await asyncio.wait_for(task, 4)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class HandoffCliTests(unittest.TestCase):
    def test_end_to_end_both_seats_modes_profile_replacement_and_log_authors(self):
        for seat in ('red', 'blue'):
            for mode, policy in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
                with self.subTest(seat=seat, mode=mode, policy=policy), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    peer = 'blue' if seat == 'red' else 'red'
                    config = root / 'config.toml'
                    config.write_text('[app]\nretry_delay_seconds=0\nstream=true\n'
                        f'[match]\nbest_of=3\ninitiative="{mode}"\nmove_policy="{policy}"\n'
                        '[profiles.human]\ntype="human"\nname="小明"\n'
                        '[profiles.other]\nadapter="openai"\nmodel="other-model"\napi_key="other-secret"\nstream=false\n'
                        '[profiles.takeover]\nadapter="deepseek"\nmodel="takeover-model"\n'
                        'name="接管者"\napi_key="takeover-secret"\nbase_url="https://takeover.invalid/v1"\n'
                        'thinking="high"\nstream=false\nstream_first_timeout_seconds=17\nstream_idle_timeout_seconds=13\n'
                        '[profiles.empty]\nadapter="openai"\napi_key=""\n'
                        f'[players.{seat}]\nprofile="human"\napi_key="old-human-secret"\n'
                        f'[players.{peer}]\nprofile="other"\n', encoding='utf-8')
                    bodies = []

                    def handler(request):
                        body = json.loads(request.content)
                        bodies.append(body)
                        ctx = json.loads(body['messages'][-1]['content'])
                        # 终端中的旧席位覆盖项不应污染接管 profile。
                        self.assertFalse(body.get('stream', False))
                        if body['model'] == 'takeover-model':
                            self.assertEqual(request.headers['authorization'], 'Bearer takeover-secret')
                            self.assertEqual(request.url.host, 'takeover.invalid')
                            self.assertEqual(body['thinking'], {'type': 'enabled'})
                            self.assertEqual(body['reasoning_effort'], 'high')
                            self.assertIn('HUMAN-PRIVATE', json.dumps(body))
                            self.assertNotIn('PRIVATE-DRAFT', json.dumps(body))
                        else:
                            self.assertEqual(body['model'], 'other-model')
                            if ctx['phase'] != 'postmatch':
                                self.assertNotIn('HUMAN-PRIVATE', json.dumps(body))
                        if ctx['phase'] == 'postmatch':
                            payload = postmatch_payload('再见 takeover-secret')
                        elif ctx['phase'] == 'chat':
                            payload = {'actions': []}
                        else:
                            payload = turn_payload([{'type': 'MOVE', 'move':
                                'Paper' if body['model'] == 'takeover-model' else 'Rock'}], 'MODEL-PRIVATE')
                        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                            'message': {'content': json.dumps(payload), 'reasoning_content': 'NATIVE-PRIVATE'}}]})

                    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                    with patch('model_rps.cli.httpx.AsyncClient', return_value=client), \
                            patch('model_rps.cli.HumanTerminal', HandoffTerminal), \
                            contextlib.redirect_stdout(io.StringIO()) as output, \
                            patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True):
                        status = main(['--config', str(config), '--log-dir', str(root / 'logs'),
                            f'--{seat}-model', 'old-model', f'--{seat}-base-url', 'https://old.invalid/v1',
                            f'--{seat}-thinking', 'off', f'--{seat}-stream', f'--{seat}-stream-idle-timeout', '99'])
                    self.assertEqual(status, 0)
                    self.assertIn(f'{seat.title()} · 接管者 赢得 BO3', output.getvalue())
                    self.assertIn('已交接给', output.getvalue())
                    run, = (root / 'logs').iterdir()
                    all_logs = '\n'.join(path.read_text(encoding='utf-8') for path in run.rglob('*.log'))
                    for secret in ('takeover-secret', 'other-secret', 'old-human-secret'):
                        self.assertNotIn(secret, all_logs)
                        self.assertNotIn(secret, output.getvalue())
                    chat = (run / 'chat.log').read_text(encoding='utf-8')
                    self.assertNotIn('HUMAN-PRIVATE', chat.split('赛后公开')[0])
                    self.assertNotIn('PRIVATE-DRAFT', all_logs)
                    self.assertNotIn('NATIVE-PRIVATE', chat)
                    self.assertIn(f'{seat.title()} · 小明：OLD-HUMAN-CHAT', chat)
                    self.assertIn(f'{seat.title()} · 小明｜动作：', chat)
                    rows = [json.loads(line) for line in (run / seat / 'debug.log').read_text(encoding='utf-8').splitlines()]
                    handoff, = [r for r in rows if r['kind'] == 'handoff']
                    self.assertEqual(handoff['profile'], 'takeover')
                    self.assertEqual(handoff['previous_player'], f'{seat.title()} · 小明')
                    self.assertTrue(any(r['kind'] == 'decision' and r['player'] == f'{seat.title()} · 小明' for r in rows))
                    self.assertTrue(any(r['kind'] == 'decision' and r['player'] == f'{seat.title()} · 接管者' for r in rows))


class HandoffTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_terminal_stops_human_reader_but_keeps_match_display(self):
        with create_pipe_input() as pipe:
            human = HumanPlayer()
            replacement = ScriptedPlayer([json.dumps(turn_payload([{'type': 'MOVE', 'move': 'Paper'}]))])
            other = ScriptedPlayer([json.dumps(turn_payload([{'type': 'MOVE', 'move': 'Rock'}]))])
            seat = SeatPlayer(human, 'Red · 小明',
                              prepare_handoff=lambda _: Replacement(replacement, 'Red · 模型', 'new'))
            seat.start_game({'game': 1})
            console = Console({'A': 'Red · 小明', 'B': 'Blue · 对手'}, color=False)
            lines = []
            console.output = lines.append
            terminal = HumanTerminal(human, 'A', console,
                session=PromptSession(input=pipe, output=DummyOutput(), erase_when_done=True))
            session = GameSession(seat, other, state=GameState(), policy=RequestPolicy(timeout=2, retry_delay=0),
                                  on_event=console.event)
            task = asyncio.create_task(terminal.run(session.run()))
            try:
                await until(lambda: terminal.session.app.is_running and human._available.is_set())
                pipe.send_text('/handoff new\n')
                result = await asyncio.wait_for(task, 4)
                self.assertEqual(result.result.winner, 'A')
                self.assertTrue(human.exiting)
                self.assertTrue(any('已交接给' in line for line in lines))
                self.assertIsNone(human._future)
                self.assertFalse(session.pending)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
