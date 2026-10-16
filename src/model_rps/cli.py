"""命令行展示与启动；默认调用真实模型，demo 模式用于模拟对局。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .game import MovePolicy
from .console import Console, display_text
from .debug_console import open_debug_window
from .player_factory import load_keys, model_players
from .config import CONFIG_PATH, RuntimeConfig, load_config, parser_defaults
from .diagnostics import LogError
from .logs import RunLogs
from .identities import player_names
from .match import MatchConfig, MatchStatus, run_match
from .players import MODEL_ADAPTERS, demo_players
from .postmatch import MatchArchive, run_reviews
from .runner import RequestPolicy


def make_parser(config: dict | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Model RPS：两个独立模型的猜拳博弈",
                                     epilog="优先级：命令行 > TOML > 内置值。以下默认值指内置值；配置可覆盖。")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path, help="TOML 配置文件，默认读取工作目录的 config.toml")
    source.add_argument("--no-config", action="store_true", help="忽略 TOML，使用内置默认值和命令行参数")
    parser.add_argument("--mode", choices=("demo", "live"), default="live",
                        help="live 真实模型（默认）；demo 模拟对局")
    parser.add_argument('--debug', action='store_true', help='额外打开独立调试窗口；前台与普通模式相同（Windows）')
    parser.add_argument("--move-policy", choices=tuple(MovePolicy), default=MovePolicy.LOCKED.value,
                        help="locked 提交后锁定（默认）；revisable 允许改拳，下一 Tick 结算")
    parser.add_argument("--scenario", choices=("locked", "change", "settlement", "draw", "forfeit", "deadline", "retry"),
                        help="模拟场景，默认随出拳规则选择 locked 或 change")
    parser.add_argument("--secrets-file", type=Path, help="兼容旧版：用指定 Python 文件中的两个密钥覆盖 TOML 密钥")
    parser.add_argument("--timeout", type=float, default=RequestPolicy().timeout, help="每次请求的总超时秒数")
    parser.add_argument('--stream', action=argparse.BooleanOptionalAction, default=False,
                        help='启用流式响应；--no-stream 使用非流式（默认），对局与复盘共用')
    parser.add_argument('--stream-first-timeout', type=float, default=60, help='流式首包等待上限秒数，默认 60')
    parser.add_argument('--stream-idle-timeout', type=float, default=30, help='流式数据事件之间的等待上限秒数，默认 30')
    parser.add_argument("--retry-delay", type=float, default=1, help="真实模式重试间隔秒数，默认 1")
    parser.add_argument("--max-output-tokens", type=int, default=20000, help="对局与复盘单次输出上限，默认 20000")
    parser.add_argument("--initiative", choices=("simultaneous", "alternating"), default="simultaneous",
                        help="simultaneous 同时决策；alternating 实验性轮流行动，仅支持 locked")
    parser.add_argument("--bo", type=int, default=1, help="正奇数；BO N 先赢 N//2+1 局，默认 BO1")
    parser.add_argument("--max-games", type=int, help="包含平局的实际总局数上限，默认 10×BO")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="日志根目录，默认 logs")
    for seat, old, adapter in (("red", "openai", "openai"), ("blue", "deepseek", "deepseek")):
        parser.add_argument(f"--{seat}-adapter", choices=tuple(MODEL_ADAPTERS), default=adapter,
                            help=f"{seat.title()} 使用的请求参数方案，与模型名和席位无关")
        for key in ("base-url", "model", "name", "thinking"):
            parser.add_argument(f"--{seat}-{key}", f"--{old}-{key}", dest=f"{seat}_{key.replace('-', '_')}",
                                default="off" if key == "thinking" else None,
                                help=f"{seat.title()} 的 {key}；旧参数 --{old}-{key} 仍兼容")
        parser.set_defaults(**{f"{seat}_api_key": None})
    if config:
        parser.set_defaults(**parser_defaults(config))
    return parser


async def play(args: argparse.Namespace) -> int:
    # 显式选择旧密钥文件时，配置中的密钥不参与校验或回退。
    if args.secrets_file is not None:
        args = argparse.Namespace(**{**vars(args), "red_api_key": None, "blue_api_key": None})
    settings = RuntimeConfig.from_args(args)
    policy = RequestPolicy(timeout=settings.timeout_seconds,
                           retry_delay=0 if settings.mode == "demo" else settings.retry_delay_seconds)
    config = MatchConfig(settings.best_of, settings.max_games, MovePolicy(settings.move_policy), settings.initiative)
    thinking = {"A": settings.red.thinking, "B": settings.blue.thinking}
    if settings.mode == "demo" and any(mode != "off" for mode in thinking.values()):
        raise ValueError("思考开关仅用于真实模型；demo 使用预设脚本，请保持双方思考为 off。")
    scenario = args.scenario or ("locked" if config.move_policy == MovePolicy.LOCKED else "change")
    if settings.mode == "demo" and scenario in ("change", "settlement") and config.move_policy == MovePolicy.LOCKED:
        raise ValueError("change 和 settlement 场景需要 --move-policy revisable。")

    async def execute(factory, names, logs):
        console = Console(names, logs)
        if args.debug:
            open_debug_window(logs)
        players = factory()
        archive = MatchArchive()
        print(f"日志目录：{display_text(str(logs.path.resolve()))}", flush=True)
        console.write(f"System：{'模拟' if settings.mode == 'demo' else '真实模型'}比赛 BO{config.best_of}，"
                      f"先胜 {config.target_wins} 局；实际最多 {config.max_games} 局。")
        console.write(f"System：参赛模型：{console.names['A']} vs {console.names['B']}。")
        console.write("System：实验性 alternating：双方轮流行动，每方最多 20 次；每局交换先手。"
                      if settings.initiative == "alternating" else "System：simultaneous：双方按同一 Tick 开始状态同时行动。")
        console.write("System：出拳规则 locked：首次提交后锁定；双方提交齐全的当前 Tick 结束时结算。"
                      if config.move_policy == MovePolicy.LOCKED else
                      "System：出拳规则 revisable：允许改拳；双方提交齐全后的下一 Tick 结算。")
        console.write(f"System：思考设置 {console.names['A']}={thinking['A']}，{console.names['B']}={thinking['B']}（对局与复盘均适用）。")
        logs.lifecycle("match_start", best_of=config.best_of, max_games=config.max_games,
                       target_wins=config.target_wins, players=names, mode=settings.mode,
                       timeout=policy.timeout, retry_delay=policy.retry_delay, move_policy=config.move_policy,
                       thinking=thinking, initiative=settings.initiative,
                       max_output_tokens=settings.max_output_tokens, stream=settings.stream, debug=args.debug,
                       stream_first_timeout_seconds=settings.stream_first_timeout_seconds,
                       stream_idle_timeout_seconds=settings.stream_idle_timeout_seconds)

        def start(number):
            console.game, console.tick = number, 0
            console.write(f"\nSystem：第 {number} 局开始，Tick 与出拳状态已重置；保留本场比赛上下文。")
            first = ("A" if number % 2 else "B") if settings.initiative == "alternating" else None
            if first:
                console.write(f"System：本局先手：{console.names[first]}。")
            logs.lifecycle("game_start", game=number, first_player=first, initiative=settings.initiative)

        def end(number, state, wins, draws):
            archive.finish_game(number, state)
            console.result(state)
            console.write(f"System：比分 {console.names['A']} {wins[0]} : {wins[1]} {console.names['B']}；平局 {draws} 局。")
            logs.lifecycle("game_end", game=number, reason=state.result.reason,
                           winner=state.result.winner, wins=wins, draws=draws,
                           completed_ticks=state.completed_ticks, turns={"A": state.turns[0], "B": state.turns[1]})

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
                    if decision["ignored_actions"]:
                        console.write(f"{label}｜锁定后忽略的 MOVE：{display_text(json.dumps(decision['ignored_actions'], ensure_ascii=False))}")
            console.game, console.tick = 0, 0
            console.write("\nSystem：赛后复盘开始，双方各自分析完整记录。")
            logs.lifecycle("review_start")
            outcomes = await run_reviews(players, report, policy=policy,
                                         on_request=lambda record: request(0, record))
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
    if settings.red.api_key is not None or settings.blue.api_key is not None:
        if not settings.red.api_key or not settings.blue.api_key:
            raise ValueError("TOML 配置必须同时填写 Red 和 Blue 的 api_key。")
        keys = (settings.red.api_key, settings.blue.api_key)
    else:
        deepseek_key, openai_key = load_keys(args.secrets_file or Path("local_secrets.py"))
        legacy_keys = {"openai": openai_key, "deepseek": deepseek_key}
        keys = tuple(legacy_keys[player.adapter] for player in settings.seats)
    names = player_names(settings.red.display_name, settings.blue.display_name)
    async with httpx.AsyncClient(timeout=policy.timeout, follow_redirects=False) as client:
        players = model_players(client, settings, keys, names)
        with RunLogs(settings.log_dir, secrets=keys, names=names) as logs:
            return await execute(lambda: players, names, logs)


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
