import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from fixtures import postmatch_payload, turn_payload
from model_rps.cli import main, make_parser, play, validate_setup
from model_rps.config import RuntimeConfig
from model_rps.ui.human_terminal import HumanTerminal, UserInterrupted
from model_rps.ui.lobby import choose_next_match, select_profile, swap_seats
from test_human import until
from test_human_terminal import AutoTerminal


PROFILES = {
    'first': {'adapter': 'openai', 'model': 'first-model', 'api_key': 'first-secret', 'thinking': 'high'},
    'second': {'adapter': 'anthropic', 'model': 'second-model', 'api_key': 'second-secret', 'thinking': 'high',
               'base_url': 'https://second.invalid/v1', 'stream': False, 'stream_idle_timeout_seconds': 19},
    'human': {'type': 'human', 'name': '小明', 'analysis': 'optional'},
    'empty': {'adapter': 'glm', 'api_key': ''},
}


def profile_args(*extra):
    return make_parser({'profiles': PROFILES, 'red': {'profile': 'first'},
                        'blue': {'profile': 'first'}}).parse_args(list(extra))


class LobbySelectionTests(unittest.TestCase):
    def test_switch_discards_old_seat_credentials_and_overrides_but_keeps_global_settings(self):
        args = profile_args('--red-type', 'human', '--red-name', 'old-name', '--red-model', 'old-model',
                            '--red-base-url', 'https://old.invalid', '--red-thinking', 'off',
                            '--red-analysis', 'off', '--red-stream', '--red-stream-idle-timeout', '99')
        args.red_api_key = 'old-secret'
        args._red_stream_defaults = {'stream': True, 'stream_first_timeout_seconds': 123}
        args._stream_defaults = {'stream': True, 'stream_first_timeout_seconds': 31}
        changed = select_profile(args, 'red', 'second')
        settings = RuntimeConfig.from_args(changed)
        self.assertEqual((settings.red.type, settings.red.model, settings.red.base_url, settings.red.api_key,
                          settings.red.adapter, settings.red.thinking, settings.red.analysis, settings.red.name),
                         ('llm', 'second-model', 'https://second.invalid/v1', 'second-secret',
                          'anthropic', 'high', 'required', None))
        self.assertEqual(settings.streaming_for(settings.red), {'stream': False,
                         'stream_first_timeout_seconds': 31, 'stream_idle_timeout_seconds': 19})
        self.assertEqual(settings.blue.api_key, 'first-secret')
        self.assertEqual(args.red_api_key, 'old-secret')
        self.assertEqual(args._red_stream_defaults['stream_first_timeout_seconds'], 123)
        self.assertEqual(changed._player_profiles, PROFILES)

        forced = profile_args('--stream', '--stream-idle-timeout', '77')
        forced = RuntimeConfig.from_args(select_profile(forced, 'blue', 'second'))
        self.assertTrue(forced.streaming_for(forced.blue)['stream'])
        self.assertEqual(forced.blue.stream_idle_timeout_seconds, 77)

    def test_swap_moves_complete_seat_settings_including_inline_defaults(self):
        for args in (profile_args('--red-profile', 'human', '--blue-stream-idle-timeout', '91'),
                     make_parser().parse_args([])):
            with self.subTest(profile=getattr(args, 'red_profile', None)):
                args._red_stream_defaults = {'stream': True}
                original = RuntimeConfig.from_args(args)
                swapped = RuntimeConfig.from_args(swap_seats(args))
                self.assertEqual(swapped.seats, tuple(reversed(original.seats)))
                self.assertEqual(RuntimeConfig.from_args(swap_seats(swap_seats(args))), original)


class LobbyPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_choices_and_missing_keys_stay_in_lobby_without_requests(self):
        args = profile_args('--session')
        lines = []
        prompt = AsyncMock()
        prompt.prompt_async.side_effect = ['/red secret-that-is-not-a-profile', '/say switch', '/exit now',
            '/red human', '/blue human', '/start', '/blue empty', '/start',
            '/profiles', '/status', '/help', '/blue second', '/start']
        before = vars(args).copy()
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
                patch('model_rps.ui.lobby.supports_color', return_value=False), \
                patch('model_rps.cli.httpx.AsyncClient') as network:
            selected, settings = await choose_next_match(args, validate=validate_setup, session=prompt, output=lines.append)
        network.assert_not_called()
        self.assertEqual(vars(args), before)
        self.assertEqual((settings.red.type, settings.blue.model), ('human', 'second-model'))
        self.assertEqual(selected.blue_profile, 'second')
        output = '\n'.join(lines)
        self.assertIn('只支持一位', output)
        self.assertIn('无法开始', output)
        self.assertIn('human：小明', output)
        for secret in ('first-secret', 'second-secret', 'secret-that-is-not-a-profile'):
            self.assertNotIn(secret, output)

    async def test_no_registry_rematch_or_exit_and_strict_control_commands(self):
        args = make_parser().parse_args(['--mode', 'demo', '--session'])
        prompt = AsyncMock()
        prompt.prompt_async.side_effect = ['', '/profiles', '换人', '/swap extra', '/swap', '/start']
        lines = []
        selected, settings = await choose_next_match(args, validate=RuntimeConfig.from_args,
                                                     session=prompt, output=lines.append)
        self.assertEqual(settings.red.adapter, 'deepseek')
        self.assertTrue(any('未配置 profiles' in line for line in lines))
        self.assertEqual(sum('无效的大厅指令' in line for line in lines), 2)
        prompt.prompt_async.side_effect = ['/exit']
        self.assertIsNone(await choose_next_match(selected, validate=RuntimeConfig.from_args,
                                                   session=prompt, output=lines.append))

    async def test_real_prompt_handles_multiple_commands_and_releases_input(self):
        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput())
            lines = []
            task = asyncio.create_task(choose_next_match(profile_args(), validate=RuntimeConfig.from_args,
                                                         session=session, output=lines.append))
            try:
                await until(lambda: session.app.is_running)
                pipe.send_text('/red human\r')
                await until(lambda: any('Red · 小明' in line for line in lines))
                await until(lambda: session.app.is_running)
                pipe.send_text('/start\r')
                _, settings = await asyncio.wait_for(task, 2)
                self.assertEqual(settings.red.type, 'human')
                self.assertFalse(session.app.is_running)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_eof_and_ctrl_c_cancel_without_starting_a_match(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                prompt = AsyncMock()
                prompt.prompt_async.side_effect = error
                validate = AsyncMock()
                with self.assertRaises(UserInterrupted):
                    await choose_next_match(profile_args(), validate=validate, session=prompt, output=lambda _: None)
                validate.assert_not_called()

    async def test_human_input_hands_off_to_lobby_then_new_human_session(self):
        with tempfile.TemporaryDirectory() as directory, create_pipe_input() as pipe:
            terminals, lobbies = [], []

            def human_terminal(player, identity, console):
                terminal = HumanTerminal(player, identity, console,
                                         session=PromptSession(input=pipe, output=DummyOutput()))
                terminals.append(terminal)
                return terminal

            def lobby_session(**kwargs):
                session = PromptSession(input=pipe, output=DummyOutput())
                lobbies.append(session)
                return session

            args = make_parser({'profiles': {'human': PROFILES['human'], 'bot': {'adapter': 'openai'}},
                                'red': {'profile': 'human'}, 'blue': {'profile': 'bot'}}).parse_args(
                                    ['--mode', 'demo', '--session', '--log-dir', directory])
            with patch('model_rps.cli.HumanTerminal', side_effect=human_terminal), \
                    patch('model_rps.ui.lobby.PromptSession', side_effect=lobby_session), \
                    patch('model_rps.ui.lobby.create_output', return_value=DummyOutput()), \
                    patch('sys.stdin.isatty', return_value=True), contextlib.redirect_stdout(io.StringIO()) as out, \
                    patch('sys.stdout.isatty', return_value=True):
                task = asyncio.create_task(play(args))
                try:
                    for number, move in ((1, 'Rock'), (2, 'Paper')):
                        await until(lambda: len(terminals) == number and terminals[-1].session.app.is_running
                                    and terminals[-1].player.can_act)
                        terminal = terminals[-1]
                        self.assertEqual(terminal.session.default_buffer.text, '')
                        self.assertEqual(terminal.player.analysis, [])
                        pipe.send_text(f'/move {move}\r')
                        await until(lambda: terminal.player.move is not None and terminal.session.app.is_running)
                        # 模拟对手会先等待一 Tick；锁定后仍要结束后续行动。
                        while terminal.player.phase != 'postmatch':
                            tick = terminal.player.view.tick
                            pipe.send_text('/endturn\r')
                            await until(lambda: terminal.player.phase == 'postmatch' or
                                        (terminal.player.can_act and terminal.player.view.tick > tick))
                            await until(lambda: terminal.session.app.is_running)
                        pipe.send_text('/exit\r')
                        await until(lambda: len(lobbies) == number and lobbies[-1].app.is_running)
                        self.assertFalse(terminal.session.app.is_running)
                        self.assertEqual(lobbies[-1].default_buffer.text, '')
                        if number == 1:
                            pipe.send_text('/swap\r')
                            await until(lambda: 'Blue · 小明｜human' in out.getvalue() and lobbies[-1].app.is_running)
                            pipe.send_text('/start\r')
                        else:
                            pipe.send_text('/exit\r')
                    self.assertEqual(await asyncio.wait_for(task, 3), 0)
                    self.assertEqual([terminal.identity for terminal in terminals], ['A', 'B'])
                    self.assertTrue(all(not session.app.is_running for session in lobbies))
                    self.assertEqual(len(list(Path(directory).iterdir())), 2)
                except TimeoutError:
                    self.fail(out.getvalue())
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


class SessionCliTests(unittest.TestCase):
    def test_noninteractive_session_fails_before_logs_or_network(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdin.isatty', return_value=False), \
                patch('model_rps.cli.httpx.AsyncClient') as network, contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(main(['--no-config', '--mode', 'demo', '--session', '--log-dir', directory]), 2)
            self.assertEqual(list(Path(directory).iterdir()), [])
        network.assert_not_called()
        self.assertIn('--session 需要交互终端', out.getvalue())

    def test_multiple_matches_reset_history_clients_usage_and_logs_and_use_new_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            config.write_text('''[app]
retry_delay_seconds=0
[profiles.first]
adapter="openai"
model="first-model"
api_key="first-secret"
[profiles.second]
adapter="anthropic"
model="second-model"
base_url="https://second.invalid/v1"
api_key="second-secret"
thinking="high"
stream=true
stream_first_timeout_seconds=27
[players.red]
profile="first"
[players.blue]
profile="first"
''', encoding='utf-8')
            clients, calls = [], []
            real_client = httpx.AsyncClient

            def make_client(**kwargs):
                number = len(clients) + 1
                first_requests = set()

                def handler(request):
                    body = json.loads(request.content)
                    ctx = json.loads(body['messages'][-1]['content'])
                    seat = (ctx['player'] if ctx['phase'] == 'postmatch' else ctx['state']['player']).split(' · ')[0]
                    native = number == 3 and seat == 'Blue'
                    self.assertEqual(body['model'], 'second-model' if native else 'first-model')
                    self.assertEqual(body['stream'], native)
                    if native:
                        self.assertEqual(request.url.host, 'second.invalid')
                        self.assertEqual(request.headers['x-api-key'], 'second-secret')
                        self.assertEqual(body['thinking'], {'type': 'adaptive', 'display': 'summarized'})
                        self.assertEqual(body['output_config'], {'effort': 'high'})
                    else:
                        self.assertEqual(request.headers['authorization'], 'Bearer first-secret')
                    for previous in range(1, number):
                        self.assertNotIn(f'HISTORY-BO-{previous}', json.dumps(body))
                    if seat not in first_requests:
                        first_requests.add(seat)
                        self.assertEqual(len(body['messages']), 1 if native else 2)
                    payload = (postmatch_payload(f'HISTORY-BO-{number}-POST') if ctx['phase'] == 'postmatch' else
                               {'actions': []} if ctx['phase'] == 'chat' else
                               turn_payload([{'type': 'MOVE', 'move': 'Paper' if seat == 'Red' else 'Rock'}],
                                            f'HISTORY-BO-{number}'))
                    content = json.dumps(payload)
                    calls.append((number, seat, ctx['phase']))
                    if native:
                        events = [
                            {'type': 'message_start', 'message': {'role': 'assistant', 'content': [],
                                                                'usage': {'input_tokens': 10, 'output_tokens': 0}}},
                            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': content}},
                            {'type': 'content_block_stop', 'index': 0},
                            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 5}},
                            {'type': 'message_stop'},
                        ]
                        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                            content=''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode())
                    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                                                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})

                client = real_client(transport=httpx.MockTransport(handler), **kwargs)
                clients.append(client)
                return client

            commands = iter(['/start', '/blue second', '/start', '/exit'])

            class Keyboard:
                async def prompt_async(inner, *args, **kwargs):
                    self.assertTrue(all(client.is_closed for client in clients))
                    for run in (root / 'logs').iterdir():
                        rows = [json.loads(line) for line in (run / 'red' / 'debug.log').read_text(encoding='utf-8').splitlines()]
                        self.assertEqual(rows[-1]['kind'], 'run_closed')
                    return next(commands)

            with patch('model_rps.cli.httpx.AsyncClient', side_effect=make_client), \
                    patch('model_rps.cli.open_debug_window') as debug, \
                    patch('model_rps.ui.lobby.PromptSession', return_value=Keyboard()), \
                    patch('model_rps.ui.lobby.create_output', return_value=DummyOutput()), \
                    patch('sys.stdin.isatty', return_value=True), contextlib.redirect_stdout(io.StringIO()) as out, \
                    patch('sys.stdout.isatty', return_value=True):
                status = main(['--config', str(config), '--log-dir', str(root / 'logs'), '--session', '--debug'])
            self.assertEqual(status, 0)
            self.assertEqual(len(clients), 3)
            self.assertEqual(debug.call_count, 3)
            self.assertEqual(len({call.args[0].path for call in debug.call_args_list}), 3)
            self.assertEqual({number for number, _, _ in calls}, {1, 2, 3})
            runs = sorted((root / 'logs').iterdir())
            self.assertEqual(len(runs), 3)
            for number, run in enumerate(runs, 1):
                for path in run.rglob('*.log'):
                    text = path.read_text(encoding='utf-8')
                    self.assertNotIn('first-secret', text)
                    self.assertNotIn('second-secret', text)
                    self.assertNotIn('比赛大厅', text)
                    for previous in range(1, number):
                        self.assertNotIn(f'HISTORY-BO-{previous}', text)
                rows = [json.loads(line) for line in (run / 'blue' / 'debug.log').read_text(encoding='utf-8').splitlines()]
                start = next(row for row in rows if row['kind'] == 'match_start')
                self.assertIn('second' if number == 3 else 'first', start['profiles'].values())
                usages = [row for row in rows if row['kind'] == 'context_usage']
                self.assertTrue(usages)
                self.assertEqual(usages[0]['input_tokens'], 10)
                self.assertEqual(usages[0]['known_input_tokens_total'], 10)
                self.assertIsNone(usages[0]['input_tokens_delta'])
                if number == 3:
                    ends = [row for row in rows if row['kind'] == 'stream_end']
                    self.assertTrue(ends)
                    self.assertTrue(all(row['status'] == 'complete' for row in ends))
            self.assertEqual(out.getvalue().count('进入比赛大厅'), 3)

    def test_human_can_change_seat_then_be_replaced_in_both_initiatives(self):
        for initiative in ('simultaneous', 'alternating'):
            with self.subTest(initiative=initiative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'config.toml'
                config.write_text('''[profiles.human]
type="human"
name="小明"
[profiles.bot]
adapter="openai"
[players.red]
profile="human"
[players.blue]
profile="bot"
''', encoding='utf-8')
                prompt = AsyncMock()
                prompt.prompt_async.side_effect = ['/swap', '/start', '/blue bot', '/start', '/exit']
                humans = []

                def terminal(player, identity, console):
                    humans.append((player, identity))
                    return AutoTerminal(player, identity, console)

                with patch('model_rps.cli.HumanTerminal', side_effect=terminal), \
                        patch('model_rps.ui.lobby.PromptSession', return_value=prompt), \
                        patch('model_rps.ui.lobby.create_output', return_value=DummyOutput()), \
                        patch('model_rps.cli.httpx.AsyncClient') as network, \
                        patch('sys.stdin.isatty', return_value=True), contextlib.redirect_stdout(io.StringIO()), \
                        patch('sys.stdout.isatty', return_value=True):
                    status = main(['--config', str(config), '--mode', 'demo', '--session', '--initiative', initiative,
                                   '--max-games', '1', '--log-dir', str(root / 'logs')])
                self.assertEqual(status, 0)
                network.assert_not_called()
                self.assertEqual([seat for _, seat in humans], ['A', 'B'])
                self.assertIsNot(humans[0][0], humans[1][0])
                self.assertEqual(len(list((root / 'logs').iterdir())), 3)

    def test_failures_do_not_enter_lobby_and_normal_exit_keeps_last_match_status(self):
        for result in (0, 1, 3, 4):
            with self.subTest(result=result), patch('model_rps.cli.play_once', AsyncMock(return_value=result)), \
                    patch('model_rps.cli.choose_next_match', AsyncMock(return_value=None)) as lobby, \
                    patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True):
                self.assertEqual(main(['--no-config', '--mode', 'demo', '--session']), result)
                self.assertEqual(lobby.await_count, int(result in (0, 3)))
        with patch('model_rps.cli.play_once', AsyncMock(return_value=0)), \
                patch('model_rps.cli.choose_next_match', AsyncMock(side_effect=UserInterrupted)), \
                patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--no-config', '--mode', 'demo', '--session']), 130)


if __name__ == '__main__':
    unittest.main()
