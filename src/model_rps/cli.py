"""命令行展示与启动；默认运行模拟对局，live 模式才调用真实模型。"""

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
from .players import ChatCompletionPlayer, DEEPSEEK, OPENAI, demo_players
from .runner import RequestPolicy, run_game

GESTURE_NAMES = {"Rock": "石头", "Paper": "布", "Scissors": "剪刀"}


def display_text(text: str) -> str:
    """将控制字符转义，确保玩家发言不会清屏或伪造新的系统消息行。"""
    return "".join(f"\\u{ord(char):04x}" if unicodedata.category(char).startswith("C") else char
                   for char in text)


class Console:
    def __init__(self, names: dict[str, str]) -> None:
        self.names = {key: display_text(value) for key, value in names.items()}

    def event(self, event: Event) -> None:
        name = self.names.get(event.player, "System")
        if event.kind == "tick":
            print(f"\n── Tick {event.tick}/{MAX_TICKS} ──", flush=True)
        elif event.kind == "say":
            print(f"{name}：{display_text(event.text)}", flush=True)
        elif event.kind in ("commit", "revise"):
            action = "已提交出拳" if event.kind == "commit" else "已修改出拳"
            print(f"System：{name} {action}。", flush=True)
        elif event.kind == "wait":
            print(f"System：{name} 本 Tick 选择等待。", flush=True)
        elif event.kind == "reveal":
            print(f"System：{name} → {GESTURE_NAMES[event.text]}", flush=True)
        elif event.kind in ("retry", "error"):
            print(f"System：{name}，{display_text(event.text)}", flush=True)
        elif event.kind == "reminder":
            print(f"System：{event.text}", flush=True)

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
        print(f"System：{text}", flush=True)


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
    parser.add_argument("--mode", choices=("demo", "live"), default="demo", help="demo 模拟对局；live 真实模型")
    parser.add_argument("--scenario", choices=("change", "draw", "forfeit", "deadline", "retry"),
                        default="change", help="模拟场景，默认演示改拳")
    parser.add_argument("--secrets-file", type=Path, default=Path("local_secrets.py"), help="本地密钥文件路径")
    parser.add_argument("--timeout", type=float, default=RequestPolicy().timeout, help="每次请求的总超时秒数")
    parser.add_argument("--deepseek-base-url", default=DEEPSEEK.base_url)
    parser.add_argument("--openai-base-url", default=OPENAI.base_url)
    parser.add_argument("--deepseek-model", default=DEEPSEEK.model)
    parser.add_argument("--openai-model", default=OPENAI.model)
    return parser


async def play(args: argparse.Namespace) -> int:
    policy = RequestPolicy(timeout=args.timeout,
                           retry_delay=0 if args.mode == "demo" else RequestPolicy().retry_delay)
    if args.mode == "demo":
        console = Console({"A": "模拟玩家 A", "B": "模拟玩家 B"})
        print(f"模拟对局：{args.scenario}", flush=True)
        state = await run_game(*demo_players(args.scenario), policy=policy, on_event=console.event)
    else:
        deepseek_key, openai_key = load_keys(args.secrets_file)
        deepseek = replace(DEEPSEEK, base_url=args.deepseek_base_url, model=args.deepseek_model)
        openai = replace(OPENAI, base_url=args.openai_base_url, model=args.openai_model)
        console = Console({"A": deepseek.model, "B": openai.model})
        async with httpx.AsyncClient(timeout=policy.timeout, follow_redirects=False) as client:
            a = ChatCompletionPlayer(client, deepseek, deepseek_key)
            b = ChatCompletionPlayer(client, openai, openai_key)
            print("真实模型对局开始。", flush=True)
            state = await run_game(a, b, policy=policy, on_event=console.event)
    console.result(state)
    return 1 if state.result.reason == EndReason.TECHNICAL_ERROR else 0


def main(argv: list[str] | None = None) -> int:
    # Windows 重定向输出默认可能使用系统代码页；日志与终端统一使用 UTF-8。
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = make_parser().parse_args(argv)
    try:
        return asyncio.run(play(args))
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nSystem：用户中止对局。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
