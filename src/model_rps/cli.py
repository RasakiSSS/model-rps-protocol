"""命令行展示与启动；默认调用真实模型，demo 模式用于模拟对局。"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path

import httpx

from .game import EndReason, Event, GameState, MAX_TICKS, MovePolicy
from .config import CONFIG_PATH, RuntimeConfig, load_config, parser_defaults
from .diagnostics import LogError
from .logs import RunLogs
from .identities import player_names
from .match import MatchConfig, MatchStatus, run_match
from .players import (ChatCompletionPlayer, DEEPSEEK, OPENAI, DEEPSEEK_THINKING_MODES,
                      OPENAI_THINKING_MODES, configure_thinking, demo_players)
from .postmatch import MatchArchive, run_reviews
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
            if event.text == "settlement":
                self.write(f"\n── 结算 Tick {event.tick}（最后一次行动机会）──")
            else:
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
        elif event.kind in ("reminder", "settlement_pending"):
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


def make_parser(config: dict | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Model RPS：两个独立模型的猜拳博弈",
                                     epilog="优先级：命令行 > TOML > 内置值。以下默认值指内置值；配置可覆盖。")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path, help="TOML 配置文件，默认读取工作目录的 config.toml")
    source.add_argument("--no-config", action="store_true", help="忽略 TOML，使用内置默认值和命令行参数")
    parser.add_argument("--mode", choices=("demo", "live"), default="live",
                        help="live 真实模型（默认）；demo 模拟对局")
    parser.add_argument("--move-policy", choices=tuple(MovePolicy), default=MovePolicy.LOCKED.value,
                        help="locked 提交后锁定（默认）；revisable 允许改拳，下一 Tick 结算")
    parser.add_argument("--scenario", choices=("locked", "change", "settlement", "draw", "forfeit", "deadline", "retry"),
                        help="模拟场景，默认随出拳规则选择 locked 或 change")
    parser.add_argument("--secrets-file", type=Path, help="兼容旧版：用指定 Python 文件中的两个密钥覆盖 TOML 密钥")
    parser.add_argument("--timeout", type=float, default=RequestPolicy().timeout, help="每次请求的总超时秒数")
    parser.add_argument("--retry-delay", type=float, default=1, help="真实模式重试间隔秒数，默认 1")
    parser.add_argument("--max-output-tokens", type=int, default=20000, help="对局与复盘单次输出上限，默认 20000")
    parser.add_argument("--initiative", choices=("simultaneous",), default="simultaneous", help="当前仅支持同时决策")
    parser.add_argument("--bo", type=int, default=1, help="正奇数；BO N 先赢 N//2+1 局，默认 BO1")
    parser.add_argument("--max-games", type=int, help="包含平局的实际总局数上限，默认 10×BO")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="日志根目录，默认 logs")
    parser.add_argument("--deepseek-base-url", default=DEEPSEEK.base_url)
    parser.add_argument("--openai-base-url", default=OPENAI.base_url)
    parser.add_argument("--deepseek-model", default=DEEPSEEK.model)
    parser.add_argument("--openai-model", default=OPENAI.model)
    parser.add_argument("--openai-name", help="GPT 玩家显示名称，省略时使用模型标识")
    parser.add_argument("--deepseek-name", help="DeepSeek 玩家显示名称，省略时使用模型标识")
    parser.add_argument("--deepseek-thinking", choices=DEEPSEEK_THINKING_MODES, default="off",
                        help="DeepSeek 思考档位，默认 off；启用可选 low/high/max")
    parser.add_argument("--openai-thinking", choices=OPENAI_THINKING_MODES, default="off",
                        help="GPT 思考档位，默认 off；启用可选 low/medium/high/xhigh/max")
    parser.set_defaults(openai_api_key=None, deepseek_api_key=None)
    if config:
        parser.set_defaults(**parser_defaults(config))
    return parser


async def play(args: argparse.Namespace) -> int:
    # 显式选择旧密钥文件时，配置中的密钥不参与校验或回退。
    if args.secrets_file is not None:
        args = argparse.Namespace(**{**vars(args), "openai_api_key": None, "deepseek_api_key": None})
    settings = RuntimeConfig.from_args(args)
    policy = RequestPolicy(timeout=settings.timeout_seconds,
                           retry_delay=0 if settings.mode == "demo" else settings.retry_delay_seconds)
    config = MatchConfig(settings.best_of, settings.max_games, MovePolicy(settings.move_policy))
    thinking = {"A": settings.gpt.thinking, "B": settings.deepseek.thinking}
    if settings.mode == "demo" and any(mode != "off" for mode in thinking.values()):
        raise ValueError("思考开关仅用于真实模型；demo 使用预设脚本，请保持双方思考为 off。")
    scenario = args.scenario or ("locked" if config.move_policy == MovePolicy.LOCKED else "change")
    if settings.mode == "demo" and scenario in ("change", "settlement") and config.move_policy == MovePolicy.LOCKED:
        raise ValueError("change 和 settlement 场景需要 --move-policy revisable。")

    async def execute(factory, names, logs):
        console = Console(names, logs)
        players = factory()
        archive = MatchArchive()
        print(f"日志目录：{display_text(str(logs.path.resolve()))}", flush=True)
        console.write(f"System：{'模拟' if settings.mode == 'demo' else '真实模型'}比赛 BO{config.best_of}，"
                      f"先胜 {config.target_wins} 局；实际最多 {config.max_games} 局。")
        console.write(f"System：参赛模型：{console.names['A']} vs {console.names['B']}。")
        console.write("System：出拳规则 locked：首次提交后锁定；双方提交齐全的当前 Tick 结束时结算。"
                      if config.move_policy == MovePolicy.LOCKED else
                      "System：出拳规则 revisable：允许改拳；双方提交齐全后的下一 Tick 结算。")
        console.write(f"System：思考设置 {console.names['A']}={thinking['A']}，{console.names['B']}={thinking['B']}（对局与复盘均适用）。")
        logs.lifecycle("match_start", best_of=config.best_of, max_games=config.max_games,
                       target_wins=config.target_wins, players=names, mode=settings.mode,
                       timeout=policy.timeout, retry_delay=policy.retry_delay, move_policy=config.move_policy,
                       thinking=thinking, initiative=settings.initiative,
                       max_output_tokens=settings.max_output_tokens)

        def start(number):
            console.game, console.tick = number, 0
            console.write(f"\nSystem：第 {number} 局开始，Tick 与出拳状态已重置；保留本场比赛上下文。")
            logs.lifecycle("game_start", game=number)

        def end(number, state, wins, draws):
            archive.finish_game(number, state)
            console.result(state)
            console.write(f"System：比分 {console.names['A']} {wins[0]} : {wins[1]} {console.names['B']}；平局 {draws} 局。")
            logs.lifecycle("game_end", game=number, reason=state.result.reason,
                           winner=state.result.winner, wins=wins, draws=draws,
                           completed_ticks=state.completed_ticks)

        def request(number, record):
            logs.request(number, record)
            archive.request(number, record)

        result = await run_match(lambda: players, config=config, policy=policy, on_start=start,
                                 on_event=lambda number, event: console.event(event),
                                 on_request=request, on_end=end)
        if result.status == MatchStatus.WON:
            console.write(f"System：比赛结束，{console.names[result.winner]} 赢得 BO{config.best_of}。")
        elif result.status == MatchStatus.UNDECIDED:
            console.write("System：达到实际总局数上限，比赛未决。")
        else:
            console.write("System：技术异常中止整场比赛，保留此前比分。")
        logs.lifecycle("match_end", status=result.status, winner=result.winner,
                       wins=result.wins, draws=result.draws, games_played=result.games_played)
        if result.status != MatchStatus.TECHNICAL_ERROR:
            report = logs.redact_value(archive.report(config, result))
            console.game, console.tick = 0, 0
            console.write("\nSystem：赛后公开双方决策说明（按局号、Tick 和玩家排列）。")
            logs.lifecycle("postmatch_disclosure_start")
            for game in report["games"]:
                for decision in game["decisions"]:
                    console.game, console.tick = decision["game"], decision["tick"]
                    label = f"第 {console.game} 局 Tick {console.tick} {console.names[decision['player']]}"
                    actions = json.dumps(decision["actions"], ensure_ascii=False)
                    analysis = decision["summary"] or "未提供正文分析；接口思考已记录于私有日志。"
                    console.write(f"{label}｜动作：{display_text(actions)}｜分析：{display_text(analysis)}")
            console.game, console.tick = 0, 0
            console.write("\nSystem：赛后复盘开始，双方各自分析完整记录。")
            logs.lifecycle("review_start")
            outcomes = await run_reviews(players, report, policy=policy,
                                         on_request=lambda record: logs.request(0, record))
            for outcome in outcomes:
                name = console.names[outcome.player]
                if outcome.review is None:
                    console.write(f"System：{name}，{display_text(outcome.error)}")
                    continue
                for label, text in (("整体评价", outcome.review.assessment),
                                    ("关键决策", outcome.review.key_moments),
                                    ("改进建议", outcome.review.improvements)):
                    console.write(f"{name} 复盘·{label}：{display_text(text)}")
            complete = all(outcome.review is not None for outcome in outcomes)
            logs.lifecycle("review_end", complete=complete,
                           players={outcome.player: "complete" if outcome.review else "failed" for outcome in outcomes})
            console.write("System：赛后复盘结束。" if complete else "System：赛后复盘部分未完成，已保存成功结果。")
            if not complete:
                return 4
        return {MatchStatus.WON: 0, MatchStatus.UNDECIDED: 3, MatchStatus.TECHNICAL_ERROR: 1}[result.status]

    if settings.mode == "demo":
        names = player_names("模拟玩家1", "模拟玩家2")
        with RunLogs(settings.log_dir, names=names) as logs:
            return await execute(lambda: demo_players(scenario),
                                 names, logs)
    if settings.gpt.api_key is not None or settings.deepseek.api_key is not None:
        if not settings.gpt.api_key or not settings.deepseek.api_key:
            raise ValueError("TOML 配置必须同时填写两个模型的 api_key。")
        keys = (settings.deepseek.api_key, settings.gpt.api_key)
    else:
        keys = load_keys(args.secrets_file or Path("local_secrets.py"))
    deepseek = configure_thinking(replace(DEEPSEEK, base_url=settings.deepseek.base_url, model=settings.deepseek.model),
                                 settings.deepseek.thinking)
    openai = configure_thinking(replace(OPENAI, base_url=settings.gpt.base_url, model=settings.gpt.model),
                               settings.gpt.thinking)
    names = player_names(settings.gpt.display_name, settings.deepseek.display_name)
    async with httpx.AsyncClient(timeout=policy.timeout, follow_redirects=False) as client:
        def factory():
            return (ChatCompletionPlayer(client, openai, keys[1], names=names, max_output_tokens=settings.max_output_tokens),
                    ChatCompletionPlayer(client, deepseek, keys[0], names=names, max_output_tokens=settings.max_output_tokens))

        factory()  # 写日志或发请求之前先校验模型配置。
        with RunLogs(settings.log_dir, secrets=keys, names=names) as logs:
            return await execute(factory, names, logs)


def main(argv: list[str] | None = None) -> int:
    # Windows 重定向输出默认可能使用系统代码页；日志与终端统一使用 UTF-8。
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    try:
        probe = argparse.ArgumentParser(add_help=False)
        source = probe.add_mutually_exclusive_group()
        source.add_argument("--config", type=Path)
        source.add_argument("--no-config", action="store_true")
        config_args, _ = probe.parse_known_args(raw_argv)
        config = {} if config_args.no_config or "--help" in raw_argv or "-h" in raw_argv else load_config(
            config_args.config or CONFIG_PATH, required=config_args.config is not None)
        args = make_parser(config).parse_args(raw_argv)
        return asyncio.run(play(args))
    except LogError as exc:
        print(f"日志错误：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nSystem：用户中止运行。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
