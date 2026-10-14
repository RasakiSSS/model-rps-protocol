"""命令行展示与启动；默认调用真实模型，demo 模式用于模拟对局。"""

from __future__ import annotations

import argparse
import ast
import asyncio
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path

import httpx

from .game import EndReason, Event, GameState, MAX_TICKS
from .diagnostics import LogError
from .logs import RunLogs
from .match import MatchConfig, MatchStatus, run_match
from .players import ChatCompletionPlayer, DEEPSEEK, OPENAI, demo_players
from .runner import RequestPolicy

GESTURE_NAMES = {"Rock": "石头", "Paper": "布", "Scissors": "剪刀"}


def display_text(text: str) -> str:
    """将控制字符转义，确保玩家发言不会清屏或伪造新的系统消息行。"""
    return "".join(f"\\u{ord(char):04x}" if unicodedata.category(char).startswith("C") else char
                   for char in text)


class Console:
    def __init__(self, names: dict[str, str], logs: RunLogs | None = None) -> None:
        self.names = {key: display_text(value) for key, value in names.items()}
        self.logs = logs
        self.game = 0
        self.tick = 0

    def write(self, text: str) -> None:
        if self.logs is not None:
            text = self.logs.redact(text)
            self.logs.chat(text, game=self.game, tick=self.tick)
        print(text, flush=True)

    def event(self, event: Event) -> None:
        self.tick = event.tick
        name = self.names.get(event.player, "System")
        if event.kind == "tick":
            self.write(f"\n── Tick {event.tick}/{MAX_TICKS} ──")
        elif event.kind == "say":
            self.write(f"{name}：{display_text(event.text)}")
        elif event.kind in ("commit", "revise"):
            action = "已提交出拳" if event.kind == "commit" else "已修改出拳"
            self.write(f"System：{name} {action}。")
        elif event.kind == "wait":
            self.write(f"System：{name} 本 Tick 选择等待。")
        elif event.kind == "reveal":
            self.write(f"System：{name} → {GESTURE_NAMES[event.text]}")
        elif event.kind in ("retry", "error"):
            self.write(f"System：{name}，{display_text(event.text)}")
        elif event.kind == "reminder":
            self.write(f"System：{event.text}")

    def result(self, state: GameState) -> None:
        result = state.result
        if result is None:
            raise ValueError("对局尚未结束。")
        if result.reason == EndReason.TECHNICAL_ERROR:
            text = "技术异常中止；本局不计胜负。"
        elif result.reason == EndReason.NO_MOVES:
            text = "双方到期未出拳，平局。"
        elif result.reason == EndReason.FORFEIT:
            loser = "B" if result.winner == "A" else "A"
            text = f"{self.names[loser]} 到期未出拳，视为弃权；{self.names[result.winner]} 获胜。"
        elif result.winner is None:
            text = "双方拳型相同，平局。"
        else:
            text = f"{self.names[result.winner]} 获胜。"
        self.write(f"System：{text}")


def load_keys(path: Path) -> tuple[str, str]:
    """只读取两个字符串常量，不执行密钥文件中的 Python 代码。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError, UnicodeError):
        raise ValueError("无法读取密钥文件；请创建 local_secrets.py 并填写两个 API key 字符串。") from None
    keys = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
                try:
                    value = ast.literal_eval(statement.value)
                except (ValueError, TypeError, SyntaxError):
                    raise ValueError("密钥必须直接填写为字符串常量。") from None
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"请在本地密钥文件中填写 {target.id}。")
                keys[target.id] = value
    if set(keys) != {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"}:
        raise ValueError("密钥文件必须包含 DEEPSEEK_API_KEY 和 OPENAI_API_KEY 两个字符串常量。")
    return keys["DEEPSEEK_API_KEY"], keys["OPENAI_API_KEY"]


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Model RPS：两个独立模型的猜拳博弈")
    parser.add_argument("--mode", choices=("demo", "live"), default="live",
                        help="live 真实模型（默认）；demo 模拟对局")
    parser.add_argument("--scenario", choices=("change", "draw", "forfeit", "deadline", "retry"),
                        default="change", help="模拟场景，默认演示改拳")
    parser.add_argument("--secrets-file", type=Path, default=Path("local_secrets.py"), help="本地密钥文件路径")
    parser.add_argument("--timeout", type=float, default=RequestPolicy().timeout, help="每次请求的总超时秒数")
    parser.add_argument("--bo", type=int, default=1, help="正奇数；BO N 先赢 N//2+1 局，默认 BO1")
    parser.add_argument("--max-games", type=int, help="包含平局的实际总局数上限，默认 10×BO")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="日志根目录，默认 logs")
    parser.add_argument("--deepseek-base-url", default=DEEPSEEK.base_url)
    parser.add_argument("--openai-base-url", default=OPENAI.base_url)
    parser.add_argument("--deepseek-model", default=DEEPSEEK.model)
    parser.add_argument("--openai-model", default=OPENAI.model)
    return parser


async def play(args: argparse.Namespace) -> int:
    policy = RequestPolicy(timeout=args.timeout,
                           retry_delay=0 if args.mode == "demo" else RequestPolicy().retry_delay)
    config = MatchConfig(args.bo, args.max_games)

    async def execute(factory, names, logs):
        console = Console(names, logs)
        print(f"日志目录：{display_text(str(logs.path.resolve()))}", flush=True)
        console.write(f"System：{'模拟' if args.mode == 'demo' else '真实模型'}比赛 BO{config.best_of}，"
                      f"先胜 {config.target_wins} 局；实际最多 {config.max_games} 局。")
        logs.lifecycle("match_start", best_of=config.best_of, max_games=config.max_games,
                       target_wins=config.target_wins, players=names, mode=args.mode,
                       timeout=policy.timeout, retry_delay=policy.retry_delay)

        def start(number):
            console.game, console.tick = number, 0
            console.write(f"\nSystem：第 {number} 局开始，Tick 与出拳状态已重置；保留本场比赛上下文。")
            logs.lifecycle("game_start", game=number)

        def end(number, state, wins, draws):
            console.result(state)
            console.write(f"System：比分 A {wins[0]} : {wins[1]} B；平局 {draws} 局。")
            logs.lifecycle("game_end", game=number, reason=state.result.reason,
                           winner=state.result.winner, wins=wins, draws=draws,
                           completed_ticks=state.completed_ticks)

        result = await run_match(factory, config=config, policy=policy, on_start=start,
                                 on_event=lambda number, event: console.event(event),
                                 on_request=logs.request, on_end=end)
        if result.status == MatchStatus.WON:
            console.write(f"System：比赛结束，{console.names[result.winner]} 赢得 BO{config.best_of}。")
        elif result.status == MatchStatus.UNDECIDED:
            console.write("System：达到实际总局数上限，比赛未决。")
        else:
            console.write("System：技术异常中止整场比赛，保留此前比分。")
        logs.lifecycle("match_end", status=result.status, winner=result.winner,
                       wins=result.wins, draws=result.draws, games_played=result.games_played)
        return {MatchStatus.WON: 0, MatchStatus.UNDECIDED: 3, MatchStatus.TECHNICAL_ERROR: 1}[result.status]

    if args.mode == "demo":
        with RunLogs(args.log_dir) as logs:
            return await execute(lambda: demo_players(args.scenario),
                                 {"A": "模拟玩家 A", "B": "模拟玩家 B"}, logs)
    keys = load_keys(args.secrets_file)
    deepseek = replace(DEEPSEEK, base_url=args.deepseek_base_url, model=args.deepseek_model)
    openai = replace(OPENAI, base_url=args.openai_base_url, model=args.openai_model)
    async with httpx.AsyncClient(timeout=policy.timeout, follow_redirects=False) as client:
        def factory():
            return (ChatCompletionPlayer(client, deepseek, keys[0]),
                    ChatCompletionPlayer(client, openai, keys[1]))

        factory()  # 写日志或发请求之前先校验模型配置。
        with RunLogs(args.log_dir, secrets=keys) as logs:
            return await execute(factory, {"A": deepseek.model, "B": openai.model}, logs)


def main(argv: list[str] | None = None) -> int:
    # Windows 重定向输出默认可能使用系统代码页；日志与终端统一使用 UTF-8。
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = make_parser().parse_args(argv)
    try:
        return asyncio.run(play(args))
    except LogError as exc:
        print(f"日志错误：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nSystem：用户中止对局。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
