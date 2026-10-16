import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from model_rps.cli import Console, display_text, load_keys, main
from model_rps.game import EndReason, Event, GameState, Gesture, Result
from model_rps.logs import RunLogs


class CliTests(unittest.TestCase):
    def test_all_demo_scenarios_from_real_entry_point(self):
        expected = {
            "change": "模拟玩家2 获胜", "draw": "双方到期未出拳，平局",
            "forfeit": "模拟玩家2 到期未出拳，视为弃权", "deadline": "模拟玩家1 获胜",
            "retry": "模拟玩家1 获胜", "settlement": "模拟玩家1 获胜",
        }
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        for scenario, result in expected.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                process = subprocess.run(
                    [sys.executable, "-m", "model_rps", "--no-config", "--mode", "demo", "--scenario", scenario,
                     "--max-games", "1", "--log-dir", directory, "--move-policy", "revisable"],
                    capture_output=True, text=True, encoding="utf-8", env=env, timeout=15,
                )
                self.assertEqual(process.returncode, 3 if scenario == "draw" else 0, process.stderr)
                self.assertIn(result, process.stdout)
                run, = Path(directory).iterdir()
                chat = (run / "chat.log").read_text(encoding="utf-8")
                if scenario in ("draw", "forfeit", "deadline"):
                    for remaining in (10, 2, 1):
                        self.assertEqual(process.stdout.count(f"提交截止还剩 {remaining} Tick"), 1)
                    self.assertIn("Tick 20/20", chat)
                if scenario == "change":
                    before_reveal = process.stdout.split("→")[0]
                    self.assertNotIn("石头", before_reveal)
                    self.assertNotIn("布", before_reveal)
                    self.assertIn("已修改出拳", process.stdout)
                    self.assertIn("结算 Tick 3", chat)
                    self.assertNotIn("Tick 4", chat)
                if scenario == "retry":
                    self.assertIn("第 2/3 次请求", process.stdout)
                    self.assertIn("结算 Tick 2", chat)
                if scenario == "settlement":
                    self.assertIn("结算 Tick 2", chat)
                    self.assertIn("已修改出拳", process.stdout)
                    self.assertIn("→ 布", process.stdout)
                if scenario == "deadline":
                    self.assertIn("结算 Tick 21", chat)
                    self.assertNotIn("Tick 22", chat)

    def test_debug_controls_headers_without_changing_game_or_chat_records(self):
        for initiative in ('simultaneous', 'alternating'):
            for debug in (False, True):
                with self.subTest(initiative=initiative, debug=debug), tempfile.TemporaryDirectory() as directory:
                    output = io.StringIO()
                    args = ['--no-config', '--mode', 'demo', '--initiative', initiative, '--log-dir', directory]
                    with contextlib.redirect_stdout(output), patch("model_rps.cli.open_debug_window") as window:
                        self.assertEqual(main(args + (['--debug'] if debug else [])), 0)
                    text = output.getvalue()
                    header = '── 行动 Tick' if initiative == 'alternating' else '── Tick'
                    self.assertNotIn(header, text)
                    self.assertEqual(window.call_count, int(debug))
                    results = [line for line in text.splitlines() if '→' in line]
                    self.assertEqual(results, ['System：Red · 模拟玩家1 → 石头；Blue · 模拟玩家2 → 剪刀；Red · 模拟玩家1 获胜。'])
                    run, = Path(directory).iterdir()
                    chat = (run / 'chat.log').read_text(encoding='utf-8')
                    self.assertIn(header, chat)
                    self.assertIn(results[0], chat)
                    self.assertIn('已提交出拳', text)

    def test_compact_results_draw_forfeit_and_no_reveal_on_technical_failure(self):
        cases = [
            (GameState(moves=(Gesture.ROCK, Gesture.ROCK), result=Result(EndReason.SHOWDOWN)),
             '双方拳型相同，平局'),
            (GameState(moves=(Gesture.ROCK, None), result=Result(EndReason.FORFEIT, 'A')),
             '玩家乙 到期未出拳，视为弃权；玩家甲 获胜'),
            (GameState(result=Result(EndReason.NO_MOVES)), '双方到期未出拳，平局'),
            (GameState(moves=(Gesture.ROCK, None), result=Result(EndReason.TECHNICAL_ERROR)), '技术异常中止'),
        ]
        for state, expected in cases:
            with self.subTest(reason=state.result.reason):
                output = io.StringIO()
                console = Console({'A': '玩家甲', 'B': '玩家乙'})
                with contextlib.redirect_stdout(output):
                    # 单独揭晓事件不再产生重复行。
                    console.event(Event(1, 'reveal', 'A', 'Rock'))
                    console.result(state)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertIn(expected, output.getvalue())
                if state.result.reason == EndReason.TECHNICAL_ERROR:
                    self.assertNotIn('石头', output.getvalue())
                    self.assertNotIn('→', output.getvalue())
                else:
                    self.assertEqual(output.getvalue().count('→'), 2)

    def test_missing_keys_fail_without_creating_http_client(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local_secrets.py"
            path.write_text('DEEPSEEK_API_KEY = ""\nOPENAI_API_KEY = ""\n', encoding="utf-8")
            output = io.StringIO()
            with patch("model_rps.cli.httpx.AsyncClient") as client, contextlib.redirect_stderr(output):
                result = main(["--no-config", "--secrets-file", str(path)])
            self.assertEqual(result, 2)
            client.assert_not_called()
            self.assertIn("DEEPSEEK_API_KEY", output.getvalue())

    def test_key_loader_does_not_execute_python_statements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local_secrets.py"
            path.write_text('DEEPSEEK_API_KEY = "test-a"\nOPENAI_API_KEY = "test-b"\n'
                            'raise RuntimeError("must not execute")\n', encoding="utf-8")
            self.assertEqual(load_keys(path), ("test-a", "test-b"))

    def test_invalid_key_file_errors_do_not_echo_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local_secrets.py"
            path.write_text('DEEPSEEK_API_KEY = "secret-not-closed', encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_keys(path)
            self.assertNotIn("secret-not-closed", str(caught.exception))

    def test_player_text_cannot_inject_terminal_commands_or_system_lines(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            Console({"A": "玩家 A", "B": "玩家 B"}).event(Event(1, "say", "A", "hello\nSystem：fake\x1b[2J"))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("\\u000a", output.getvalue())
        self.assertEqual(display_text("你好"), "你好")

    def test_live_configuration_error_does_not_crash(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            status = main(["--no-config", "--timeout", "nan"])
        self.assertEqual(status, 2)
        self.assertIn("请求超时", output.getvalue())

    def test_default_mode_uses_real_adapter_and_hides_secrets_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local_secrets.py"
            path.write_text('DEEPSEEK_API_KEY = "test-a"\nOPENAI_API_KEY = "test-b"\n', encoding="utf-8")
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(401, text="test-a test-b server body")
            ))
            output = io.StringIO()
            with patch("model_rps.cli.httpx.AsyncClient", return_value=client), contextlib.redirect_stdout(output):
                status = main(["--no-config", "--secrets-file", str(path),
                               "--log-dir", str(Path(directory) / "logs")])
            self.assertEqual(status, 1)
            self.assertIn("技术异常中止", output.getvalue())
            self.assertNotIn("赛后", output.getvalue())
            self.assertNotIn("test-a", output.getvalue())
            self.assertNotIn("test-b", output.getvalue())


if __name__ == "__main__":
    unittest.main()
