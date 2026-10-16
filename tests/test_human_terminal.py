import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from fixtures import postmatch_payload, turn_payload
from model_rps.cli import main
from model_rps.console import Console
from model_rps.game import GameState
from model_rps.human import HumanPlayer
from model_rps.human_terminal import HumanTerminal, UserInterrupted
from model_rps.identities import player_names
from model_rps.logs import RunLogs
from test_human import until


class AutoTerminal:
    """只替代键盘；玩家、调度、校验、HTTP 与日志仍走生产链路。"""

    def __init__(self, player, identity, console):
        self.player = player

    async def run(self, match):
        game = asyncio.create_task(match)
        player = self.player
        spoke = set()

        async def keyboard():
            while not game.done():
                await until(lambda: game.done() or player._available.is_set())
                if game.done():
                    break
                if player.phase == 'postmatch':
                    await player.submit('/exit')
                    return
                if player.can_act:
                    if player.epoch not in spoke:
                        await player.submit('/analyze HUMAN-PRIVATE')
                        await player.submit('/move Paper')
                        await player.submit('/say 先聊一句')
                        spoke.add(player.epoch)
                    else:
                        await player.submit('/endturn')
                else:
                    await asyncio.sleep(.001)

        reader = asyncio.create_task(keyboard())
        try:
            done, _ = await asyncio.wait((game, reader), return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                reader.result()
            return await asyncio.wait_for(game, 4)
        finally:
            game.cancel()
            reader.cancel()
            await asyncio.gather(game, reader, return_exceptions=True)


class HumanCliTests(unittest.TestCase):
    def test_live_bo_human_either_seat_public_privacy_and_only_one_model_request_stream(self):
        for seat in ('red', 'blue'):
            for mode, policy in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
                with self.subTest(seat=seat, mode=mode, policy=policy), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = root / 'config.toml'
                    peer = 'blue' if seat == 'red' else 'red'
                    config.write_text('[app]\nretry_delay_seconds=0\n[match]\nbest_of=3\n'
                                      f'initiative="{mode}"\nmove_policy="{policy}"\n'
                                      f'[players.{seat}]\ntype="human"\nname="小明"\napi_key=""\n'
                                      f'[players.{peer}]\nmodel="only-model"\napi_key="model-test-key"', encoding='utf-8')
                    calls = []

                    def handler(request):
                        body = json.loads(request.content)
                        calls.append(body)
                        self.assertEqual(body['model'], 'only-model')
                        self.assertEqual(request.headers['authorization'], 'Bearer model-test-key')
                        ctx = json.loads(body['messages'][-1]['content'])
                        if ctx['phase'] == 'postmatch':
                            self.assertIn('HUMAN-PRIVATE', json.dumps(ctx))
                            payload = postmatch_payload('下次再聊')
                        else:
                            self.assertNotIn('HUMAN-PRIVATE', json.dumps(body))
                            payload = {'actions': []} if ctx['phase'] == 'chat' else turn_payload(
                                [{'type': 'MOVE', 'move': 'Rock'}], 'MODEL-PRIVATE')
                        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                            'content': json.dumps(payload), 'reasoning_content': 'NATIVE-PRIVATE'}}]})

                    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                    with patch('model_rps.cli.HumanTerminal', AutoTerminal), patch('sys.stdin.isatty', return_value=True), \
                            patch('model_rps.cli.httpx.AsyncClient', return_value=client), contextlib.redirect_stdout(io.StringIO()) as output, \
                            patch('sys.stdout.isatty', return_value=True):
                        self.assertEqual(main(['--config', str(config), '--log-dir', str(root / 'logs')]), 0)
                    self.assertIn(f'{seat.title()} · 小明 赢得 BO3', output.getvalue())
                    self.assertIn('双方已退出', output.getvalue())
                    run, = (root / 'logs').iterdir()
                    chat = (run / 'chat.log').read_text(encoding='utf-8')
                    self.assertNotIn('PRIVATE', chat.split('赛后公开')[0])
                    self.assertNotIn('NATIVE-PRIVATE', chat)
                    own = [json.loads(line) for line in (run / seat / 'debug.log').read_text(encoding='utf-8').splitlines()]
                    self.assertFalse(any(row['kind'] in ('http_request', 'http_response', 'context_usage') for row in own))
                    self.assertTrue(any(row['kind'] == 'decision' and row['summary'] == 'HUMAN-PRIVATE' for row in own))
                    self.assertEqual(len([body for body in calls if json.loads(body['messages'][-1]['content'])['phase'] == 'postmatch']), 1)

    def test_offline_human_game_and_invalid_terminal_fail_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ['--no-config', '--mode', 'demo', '--red-type', 'human', '--red-name', '练习玩家', '--log-dir', directory]
            with patch('model_rps.cli.HumanTerminal', AutoTerminal), patch('sys.stdin.isatty', return_value=True), \
                    patch('model_rps.cli.httpx.AsyncClient') as client, contextlib.redirect_stdout(io.StringIO()), \
                    patch('sys.stdout.isatty', return_value=True):
                self.assertEqual(main(args), 0)
            client.assert_not_called()
            for input_tty, output_tty in ((False, True), (True, False)):
                with patch('sys.stdin.isatty', return_value=input_tty), patch('sys.stdout.isatty', return_value=output_tty), \
                        patch('model_rps.cli.httpx.AsyncClient') as client, contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(main(args), 2)
                self.assertIn('交互终端', errors.getvalue())
                client.assert_not_called()


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_session_uses_same_app_and_enter_binding_with_term_dumb(self):
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()), \
                patch.dict(os.environ, {'TERM': 'dumb'}), \
                patch('model_rps.human_terminal.create_output', return_value=DummyOutput()):
            player = HumanPlayer()
            terminal = HumanTerminal(player, 'A', Console(player_names('human', 'model')))
            player.start_game({'game': 1})  # 与 CLI 一样，创建终端之后才开始本局。
            player.update_view(GameState().view_for('A'))
            task = asyncio.create_task(terminal.run(player.respond(player.view)))
            try:
                await until(lambda: terminal.session.app.is_running and player._available.is_set())
                pipe.send_text('/endturn\r')
                response = await asyncio.wait_for(task, 2)
                self.assertIn('ENDTURN', response.content)
                self.assertEqual(terminal.accepted_epoch, player.epoch)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_eof_and_ctrl_c_cancel_pending_game_and_input(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), create_pipe_input() as pipe:
                ended = asyncio.Event()

                async def match():
                    try:
                        await asyncio.Event().wait()
                    finally:
                        ended.set()

                session = PromptSession(input=pipe, output=DummyOutput())
                session.prompt_async = AsyncMock(side_effect=error)
                terminal = HumanTerminal(HumanPlayer(), 'A', Console(player_names('human', 'model')), session=session)
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(UserInterrupted):
                    await terminal.run(match())
                self.assertTrue(ended.is_set())

    async def test_real_async_prompt_keeps_draft_when_opponent_speaks_and_does_not_log_private_input(self):
        with create_pipe_input() as pipe, tempfile.TemporaryDirectory() as directory, \
                RunLogs(Path(directory), names=player_names('human', 'model')) as logs:
            player = HumanPlayer()
            player.start_game({'game': 1})
            player.update_view(GameState().view_for('A'))
            session = PromptSession(input=pipe, output=DummyOutput())
            console = Console(logs.names, logs, color=False)
            terminal = HumanTerminal(player, 'A', console, session=session)
            task = asyncio.create_task(terminal.run(player.respond(player.view)))
            try:
                await until(lambda: task.done() or (session.app.is_running and player._available.is_set()))
                if task.done():
                    await task
                pipe.send_text('/analyze PRIVATE-DRAFT')
                await until(lambda: session.default_buffer.text == '/analyze PRIVATE-DRAFT')
                console.write('Blue · model：你好')
                await asyncio.sleep(.03)
                self.assertEqual(session.default_buffer.text, '/analyze PRIVATE-DRAFT')
                pipe.send_text('\r')
                await until(lambda: len(player.analysis) == 1)
                await until(lambda: session.app.is_running)
                pipe.send_text('/move Paper\r')
                await until(lambda: player.move is not None)
                await until(lambda: session.app.is_running)
                pipe.send_text('/endturn\r')
                result = await asyncio.wait_for(task, 3)
                self.assertIn('PRIVATE-DRAFT', result.content)
                self.assertNotIn('PRIVATE-DRAFT', (logs.path / 'chat.log').read_text(encoding='utf-8'))
                self.assertIn('你好', (logs.path / 'chat.log').read_text(encoding='utf-8'))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_boundary_clears_action_text_keeps_chat_and_rejects_already_entered_old_action(self):
        player = HumanPlayer()
        player.start_game({'game': 1})
        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput())
            terminal = HumanTerminal(player, 'A', Console(player_names('human', 'model')), session=session)
            player.on_boundary = terminal._boundary
            session.default_buffer.text = '/move Paper'
            epoch = player.epoch
            player.start_game({'game': 2})
            self.assertEqual(session.default_buffer.text, '')
            with self.assertRaisesRegex(Exception, '旧输入'):
                await player.submit('/endturn', epoch=epoch)
            session.default_buffer.text = '/say 还没发出的聊天'
            player.start_game({'game': 3})
            self.assertEqual(session.default_buffer.text, '/say 还没发出的聊天')


if __name__ == '__main__':
    unittest.main()
