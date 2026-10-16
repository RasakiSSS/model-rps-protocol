import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from model_rps.cli import main
from model_rps.console import Console
from model_rps.debug_console import LogTail, ParentProcess, format_record, open_debug_window, watch
from model_rps.game import Event
from model_rps.identities import player_names
from model_rps.logs import RunLogs
from model_rps.terminal import supports_color


class DebugConsoleTests(unittest.TestCase):
    def test_parent_handle_observes_live_process_and_viewer_stops_if_parent_exits(self):
        process = ParentProcess(os.getpid())
        try:
            self.assertTrue(process.alive())
        finally:
            process.close()
        with tempfile.TemporaryDirectory() as directory:
            for seat in ('red', 'blue'):
                (Path(directory) / seat).mkdir()
                (Path(directory) / seat / 'debug.log').write_text('', encoding='utf-8')
            with patch('model_rps.debug_console.ParentProcess') as parent, contextlib.redirect_stdout(io.StringIO()) as out:
                parent.return_value.alive.return_value = False
                watch(Path(directory), parent=123)
                parent.return_value.close.assert_called_once()
            self.assertIn('读取结束', out.getvalue())

    def test_frontend_identical_and_closed_viewer_does_not_affect_game(self):
        for mode in ('simultaneous', 'alternating'):
            outputs = []
            with tempfile.TemporaryDirectory() as directory:
                for debug in (False, True):
                    with patch('model_rps.cli.open_debug_window') as window, contextlib.redirect_stdout(io.StringIO()) as out:
                        window.return_value.poll.return_value = 0  # 用户已经关闭观察窗口。
                        args = ['--no-config', '--mode', 'demo', '--bo', '3', '--initiative', mode, '--log-dir', directory]
                        self.assertEqual(main(args + (['--debug'] if debug else [])), 0)
                    self.assertEqual(window.call_count, int(debug))
                    outputs.append('\n'.join(out.getvalue().splitlines()[1:]))  # 唯一变化是运行目录。
                self.assertEqual(outputs[0], outputs[1])

    def test_window_launch_uses_argument_list_and_only_log_path_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory, RunLogs(Path(directory), secrets=('do-not-pass',)) as logs:
            with patch('model_rps.debug_console.sys.platform', 'win32'), patch('model_rps.debug_console.subprocess.Popen') as popen:
                with patch('model_rps.debug_console.subprocess.CREATE_NEW_CONSOLE', 16, create=True):
                    self.assertIs(open_debug_window(logs), popen.return_value)
                args, kwargs = popen.call_args
                self.assertIn(str(logs.path.resolve()), args[0])
                self.assertIn('--window', args[0])
                self.assertNotIn('do-not-pass', repr(popen.call_args))
                self.assertFalse(kwargs.get('shell', False))
                self.assertEqual(kwargs['creationflags'], 16)
            with patch('model_rps.debug_console.subprocess.Popen', side_effect=OSError('sensitive detail')), \
                    patch('model_rps.debug_console.sys.platform', 'win32'), contextlib.redirect_stderr(io.StringIO()) as out:
                with patch('model_rps.debug_console.subprocess.CREATE_NEW_CONSOLE', 16, create=True):
                    self.assertIsNone(open_debug_window(logs))
                self.assertNotIn('sensitive detail', out.getvalue())
                self.assertIn('比赛继续', out.getvalue())

    def test_incremental_utf8_partial_lines_and_large_records_are_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'debug.log'
            row = {'kind': 'usage', 'text': '中文'}
            data = (json.dumps(row, ensure_ascii=False) + '\n').encode()
            split = data.index('中'.encode()) + 1
            path.write_bytes(data[:split])
            tail = LogTail(path)
            self.assertEqual(tail.read(), [])
            with path.open('ab') as file:
                file.write(data[split:])
            self.assertEqual(tail.read(), [row])
            self.assertEqual(tail.read(), [])
            for seat in ('red', 'blue'):
                (Path(directory) / seat).mkdir()
                (Path(directory) / seat / 'debug.log').write_text(
                    json.dumps({'kind': 'http_request', 'payload': 'x' * 150000}) + '\n' +
                    json.dumps({'kind': 'run_closed', 'status': 'complete'}) + '\n', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                watch(Path(directory), once=True)
            self.assertEqual(output.getvalue().count('run_closed'), 1)

    def test_viewer_shows_progress_duration_and_redacted_usage_without_private_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunLogs(Path(directory), names=player_names('same', 'same'), secrets=('secret-key',)) as logs:
                logs.lifecycle('match_start', players=logs.names)
                console = Console(logs.names, logs, color=False)
                console.event(Event(2, 'tick'))
                for player in ('A', 'B'):
                    base = {'player': player, 'tick': 2, 'attempt': 1, 'phase': 'postmatch'}
                    logs.request(0, {**base, 'kind': 'stream_status', 'status': 'thinking'})
                    logs.request(0, {**base, 'kind': 'raw_response', 'content': 'PRIVATE-THOUGHT'})
                    logs.request(0, {**base, 'kind': 'usage', 'usage': {'prompt_tokens': 55, 'note': 'secret-key'}})
                    logs.request(0, {**base, 'kind': 'request_end', 'status': 'valid', 'elapsed_seconds': 1.23})
            with contextlib.redirect_stdout(io.StringIO()) as output:
                watch(logs.path)
            text = output.getvalue()
            for expected in ('Red · same', 'Blue · same', 'Tick 2/20', '接收思考', '赛后聊天', '1.23 秒', 'prompt_tokens', '[REDACTED]'):
                self.assertIn(expected, text)
            self.assertNotIn('PRIVATE-THOUGHT', text)
            self.assertNotIn('secret-key', text)
            self.assertEqual(text.count('run_closed'), 1)
            self.assertNotIn('\x1b', format_record({'kind': 'usage', 'note': '\x1b[2J\n伪造'}))

    def test_color_only_on_terminal_and_logs_remain_plain(self):
        names = player_names('same', 'same')
        with tempfile.TemporaryDirectory() as directory, RunLogs(Path(directory), names=names) as logs:
            console = Console(names, logs, color=True)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                console.event(Event(1, 'say', 'A', '你好\x1b[2J'))
                console.event(Event(1, 'commit', 'B'))
            self.assertIn('\x1b[91mRed · same\x1b[0m', output.getvalue())
            self.assertIn('\x1b[94mBlue · same\x1b[0m', output.getvalue())
            self.assertNotIn('\x1b[2J', output.getvalue())
            self.assertNotIn('\x1b', (logs.path / 'chat.log').read_text(encoding='utf-8'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(supports_color())
        with patch.dict(os.environ, {'NO_COLOR': ''}), patch('sys.stdout.isatty', return_value=True):
            self.assertFalse(supports_color())

    def test_chat_opportunity_silence_and_discard_are_distinct_in_viewer(self):
        base = {'player': 'Blue · same', 'game': 1, 'tick': 1, 'request_id': 2, 'phase': 'chat'}
        start = format_record({**base, 'kind': 'request_start', 'attempt': 1,
                               'state': {'request_reasons': ['game_start', 'opponent_say']}})
        self.assertIn('触发：开局、对手发言', start)
        self.assertIn('聊天 · 响应 #2', start)
        silent = format_record({**base, 'kind': 'interaction_applied', 'silent': True, 'applied_tick': 2})
        self.assertIn('已应用：保持沉默', silent)
        self.assertIn('应用于 Tick 2', silent)
        self.assertIn('聊天 · 响应 #2', silent)
        discarded = format_record({**base, 'kind': 'request_discarded', 'reason': '结算，历史已回退'})
        self.assertIn('响应未应用：结算，历史已回退', discarded)
        self.assertNotIn('保持沉默', discarded)


if __name__ == '__main__':
    unittest.main()
