"""命令行展示与启动；默认调用真实模型，demo 模式用于模拟对局。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .core.game import MovePolicy
from .ui.console import Console, display_text
from .ui.debug_console import open_debug_window
from .player_factory import load_keys, model_player, model_players, player_keys
from .players.human import HumanPlayer
from .ui.human_terminal import HumanTerminal, UserInterrupted, wait_for_resume
from .config import CONFIG_PATH, RuntimeConfig, load_config, parser_defaults
from .core.diagnostics import LogError
from .core.logs import RunLogs
from .core.identities import player_names
from .engine.match import MatchConfig, MatchStatus, run_match
from .players.base import demo_players
from .llm.adapters import MODEL_ADAPTERS
from .core.commands import AnalysisPolicy
from .engine.postmatch import MatchArchive, run_postmatch
from .engine.runner import RequestPolicy
from .ui.lobby import choose_next_match, select_profile
from .players.seats import Replacement, SeatPlayer


def make_parser(config: dict | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Model RPS：模型对战与人类 CLI 猜拳",
                                     epilog="优先级：命令行 > TOML > 内置值。以下默认值指内置值；配置可覆盖。")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path, help="TOML 配置文件，默认读取工作目录的 config.toml")
    source.add_argument("--no-config", action="store_true", help="忽略 TOML，使用内置默认值和命令行参数")
    parser.add_argument("--mode", choices=("demo", "live"), default="live",
                        help="live 真实模型（默认）；demo 模拟对局")
    parser.add_argument('--debug', action='store_true', help='额外打开独立调试窗口；前台与普通模式相同（Windows）')
    parser.add_argument('--session', action='store_true',
                        help='连续比赛：赛后双方退出后选择下一场玩家；需要交互终端')
    parser.add_argument("--move-policy", choices=tuple(MovePolicy), default=MovePolicy.LOCKED.value,
                        help="locked 提交后锁定（默认）；revisable 允许改拳，下一 Tick 结算")
    parser.add_argument("--scenario", choices=("locked", "change", "settlement", "draw", "forfeit", "deadline", "retry", "chat"),
                        help="模拟场景，默认随出拳规则选择 locked 或 change")
    parser.add_argument("--secrets-file", type=Path, help="兼容旧版：从指定 Python 文件读取模型席位所需密钥，覆盖 TOML 密钥")
    parser.add_argument("--timeout", type=float, default=RequestPolicy().timeout, help="每次模型请求的总超时秒数；不限制人类输入等待")
    parser.add_argument('--stream', action=argparse.BooleanOptionalAction, default=None,
                        help='覆盖双方流式设置；--no-stream 关闭；未指定时按席位/profile/app 继承，内置 false')
    parser.add_argument('--stream-first-timeout', type=float, default=None, help='覆盖双方流式首包等待上限秒数，内置 60')
    parser.add_argument('--stream-idle-timeout', type=float, default=None, help='覆盖双方流式数据事件间的等待上限秒数，内置 30')
    parser.add_argument("--retry-delay", type=float, default=1, help="真实模式重试间隔秒数，默认 1")
    parser.add_argument("--max-output-tokens", type=int, default=20000, help="对局与赛后聊天单次输出上限，默认 20000")
    parser.add_argument("--initiative", choices=("simultaneous", "alternating"), default="simultaneous",
                        help="simultaneous 同时决策；alternating 实验性轮流行动，仅支持 locked")
    parser.add_argument("--bo", type=int, default=1, help="正奇数；BO N 先赢 N//2+1 局，默认 BO1")
    parser.add_argument("--max-games", type=int, help="包含平局的实际总局数上限，默认 10×BO")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="日志根目录，默认 logs")
    for seat, old in (("red", "openai"), ("blue", "deepseek")):
        parser.add_argument(f"--{seat}-profile", help=f"{seat.title()} 使用的 [profiles.名称]；兼容 [models.名称]")
        parser.add_argument(f"--{seat}-stream", action=argparse.BooleanOptionalAction, default=None,
                            help=f"仅覆盖 {seat.title()} 的流式开关，优先于 --stream / --no-stream")
        parser.add_argument(f"--{seat}-stream-first-timeout", type=float,
                            help=f"仅覆盖 {seat.title()} 的流式首包等待上限秒数")
        parser.add_argument(f"--{seat}-stream-idle-timeout", type=float,
                            help=f"仅覆盖 {seat.title()} 的流式空闲等待上限秒数")
        parser.add_argument(f"--{seat}-type", choices=("llm", "human"), default=None,
                            help=f"{seat.title()} 玩家类型；一个终端最多一位 human，其余使用 llm")
        parser.add_argument(f"--{seat}-adapter", choices=tuple(MODEL_ADAPTERS), default=None,
                            help=f"{seat.title()} 使用的请求参数方案，与模型名和席位无关")
        parser.add_argument(f"--{seat}-analysis", choices=tuple(AnalysisPolicy), default=None,
                            help=f"{seat.title()} 的策略分析：off/optional/required；人类默认 optional，模型默认 required")
        for key in ("base-url", "model", "name", "thinking"):
            parser.add_argument(f"--{seat}-{key}", f"--{old}-{key}", dest=f"{seat}_{key.replace('-', '_')}",
                                default=None,
                                help=f"{seat.title()} 的 {key}；旧参数 --{old}-{key} 仍兼容")
        parser.set_defaults(**{f"{seat}_api_key": None})
    if config:
        parser.set_defaults(**parser_defaults(config))
    return parser


def validate_setup(args: argparse.Namespace) -> RuntimeConfig:
    """开赛前的本地校验；大厅也使用它，失败时不离开选择界面。"""
    # 显式选择旧密钥文件时，配置中的密钥不参与校验或回退。
    if args.secrets_file is not None:
        args = argparse.Namespace(**{**vars(args), "red_api_key": None, "blue_api_key": None})
    settings = RuntimeConfig.from_args(args)
    if any(player.type == "human" for player in settings.seats) and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("人类玩家需要交互终端，请在 PowerShell 或终端窗口中运行，不要重定向标准输入或输出。")
    if args.session and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("--session 需要交互终端，请不要重定向标准输入或输出；单场运行请去掉 --session。")
    thinking = {seat: player.thinking if player.type == "llm" else "off"
                for seat, player in zip(("A", "B"), settings.seats)}
    if settings.mode == "demo" and any(mode != "off" for mode in thinking.values()):
        raise ValueError("思考开关仅用于真实模型；demo 使用预设脚本，请保持双方思考为 off。")
    if settings.mode == "demo" and args.scenario in ("change", "settlement") and settings.move_policy == MovePolicy.LOCKED:
        raise ValueError("change 和 settlement 场景需要 --move-policy revisable。")
    if settings.mode == "live":
        player_keys(settings, args.secrets_file or Path("local_secrets.py"))
    return settings


async def play(args: argparse.Namespace) -> int:
    settings = validate_setup(args)
    while True:
        status = await play_once(args, settings)
        # 技术异常或取消不会被当作双方正常退出，也不自动开始下一场。
        if not args.session or status not in (0, 3):
            return status
        following = await choose_next_match(args, validate=validate_setup)
        if following is None:
            return status
        args, settings = following


async def play_once(args: argparse.Namespace, settings: RuntimeConfig) -> int:
    """一场 BO 及其赛后聊天；返回前关闭终端、日志与 HTTP 客户端。"""
    policy = RequestPolicy(timeout=settings.timeout_seconds,
                           retry_delay=0 if settings.mode == "demo" else settings.retry_delay_seconds)
    config = MatchConfig(settings.best_of, settings.max_games, MovePolicy(settings.move_policy), settings.initiative)
    thinking = {seat: player.thinking if player.type == "llm" else "off"
                for seat, player in zip(("A", "B"), settings.seats)}
    scenario = args.scenario or ("locked" if config.move_policy == MovePolicy.LOCKED else "change")

    async def execute(factory, names, logs, client=None):
        console = Console(names, logs)
        originals = factory()

        def prepare(identity, profile):
            prefix, index = ("red", 0) if identity == "A" else ("blue", 1)
            selected = getattr(args, "_player_profiles", {}).get(profile)
            if selected is None:
                raise ValueError("未找到该 profile；请使用配置文件中已有的模型条目名称。")
            if selected.get("type", "llm") != "llm":
                raise ValueError("/handoff 只能交给模型 profile。")
            candidate = select_profile(args, prefix, profile)
            following = validate_setup(candidate)
            config = following.seats[index]
            label = config.display_name if settings.mode == "live" else f"模拟（{config.display_name}）"
            name = f"{prefix.title()} · {label}"
            if settings.mode == "demo":
                player = demo_players(scenario, analysis=tuple(p.analysis for p in following.seats))[index]
            else:
                key = player_keys(following, args.secrets_file or Path("local_secrets.py"))[index]
                logs.add_secrets(key)
                player = model_player(client, following, config, key, {**names, identity: name})
            return Replacement(player, name, profile)

        def changed(identity, replacement):
            names[identity] = replacement.name
            console.rename_player(identity, replacement.name)
            logs.names[identity] = replacement.name
            for seat in players:
                if hasattr(seat.player, "names"):
                    seat.player.names.update(names)

        players = tuple(SeatPlayer(player, names[identity],
                                  prepare_handoff=lambda profile, identity=identity: prepare(identity, profile),
                                  on_handoff=lambda replacement, identity=identity: changed(identity, replacement))
                        for identity, player in zip(("A", "B"), originals))
        for identity, player in zip(("A", "B"), players):
            if getattr(player, "interactive", False):
                terminal = HumanTerminal(player.player, identity, console)
                return await terminal.run(execute_match(players, console, names, logs,
                                                        terminal.wait_for_resume))
        return await execute_match(players, console, names, logs)

    async def execute_match(players, console, names, logs, resume_prompt=None):
        if args.debug:
            open_debug_window(logs)
        archive = MatchArchive()
        console.output(f"日志目录：{display_text(str(logs.path.resolve()))}")
        console.write(f"System：{'模拟' if settings.mode == 'demo' else '真实模型'}比赛 BO{config.best_of}，"
                      f"先胜 {config.target_wins} 局；实际最多 {config.max_games} 局。")
        console.write(f"System：参赛玩家：{console.names['A']} vs {console.names['B']}。")
        console.write("System：实验性 alternating：双方轮流行动，每方最多 20 次；每局交换先手。"
                      if settings.initiative == "alternating" else "System：simultaneous：双方结束行动后同步推进，聊天即时公开。")
        console.write("System：出拳规则 locked：首次提交后锁定；双方提交齐全的当前 Tick 结束时结算。"
                      if config.move_policy == MovePolicy.LOCKED else
                      "System：出拳规则 revisable：允许改拳；双方提交齐全后的下一 Tick 结算。")
        console.write(f"System：思考设置 {console.names['A']}={thinking['A']}，{console.names['B']}={thinking['B']}（对局与赛后聊天均适用）。")
        logs.lifecycle("match_start", best_of=config.best_of, max_games=config.max_games,
                       target_wins=config.target_wins, players=names, mode=settings.mode,
                       profiles={seat: getattr(args, f"{prefix}_profile", None)
                                 for seat, prefix in (("A", "red"), ("B", "blue"))},
                       player_types={seat: player.type for seat, player in zip(("A", "B"), settings.seats)},
                       timeout=policy.timeout, retry_delay=policy.retry_delay, move_policy=config.move_policy,
                       thinking=thinking, initiative=settings.initiative,
                       analysis={"A": settings.red.analysis, "B": settings.blue.analysis},
                       max_output_tokens=settings.max_output_tokens, stream=settings.stream, debug=args.debug,
                       stream_first_timeout_seconds=settings.stream_first_timeout_seconds,
                       stream_idle_timeout_seconds=settings.stream_idle_timeout_seconds,
                       player_streaming={seat: settings.streaming_for(player)
                                         for seat, player in zip(("A", "B"), settings.seats)})

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
            record = {"actor": names[record["player"]], **record}
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
            console.write("\nSystem：赛后公开双方策略分析（按局号、Tick 和玩家排列）。")
            logs.lifecycle("postmatch_disclosure_start")
            for game in report["games"]:
                for decision in game["decisions"]:
                    console.game, console.tick = decision["game"], decision["tick"]
                    author = display_text(decision.get("actor") or console.names[decision['player']])
                    label = f"第 {console.game} 局 Tick {console.tick} {author}"
                    actions = json.dumps(decision["actions"], ensure_ascii=False)
                    analysis = decision["summary"] or "本次未提交策略分析。"
                    console.write(f"{label}｜动作：{display_text(actions)}｜分析：{display_text(analysis)}")
                    if decision["ignored_actions"]:
                        console.write(f"{label}｜锁定后忽略的 MOVE：{display_text(json.dumps(decision['ignored_actions'], ensure_ascii=False))}")
            console.game, console.tick = 0, 0
            console.write("\nSystem：赛后聊天开始，双方可自由交流；双方 EXIT 后结束。")
            logs.lifecycle("postmatch_start")

            def postmatch_event(event):
                logs.lifecycle("postmatch_event", event=event)
                name = console.names[event["player"]]
                if event["kind"] == "exit":
                    console.write(f"System：{name} 已退出赛后聊天。")
                else:
                    lines = event["text"].splitlines()
                    console.write(f"{name}：{display_text(lines[0])}")
                    for line in lines[1:]:
                        console.write(f"  {display_text(line)}")

            async def idle(exited):
                logs.lifecycle("postmatch_idle", exited=exited)
                await (resume_prompt() if resume_prompt else wait_for_resume(console))
                logs.lifecycle("postmatch_resumed", exited=exited)

            outcome = await run_postmatch(players, report, policy=policy,
                                         on_request=lambda record: request(0, record), on_event=postmatch_event,
                                         on_idle=idle)
            logs.lifecycle("postmatch_end", complete=outcome.complete, exited=outcome.exited,
                           failed_player=outcome.failed_player, error=outcome.error)
            if not outcome.complete:
                console.write(f"System：{console.names[outcome.failed_player]}，{display_text(outcome.error)}")
                return 4
            console.write("System：双方已退出，赛后聊天结束。")
        return {MatchStatus.WON: 0, MatchStatus.UNDECIDED: 3, MatchStatus.TECHNICAL_ERROR: 1}[result.status]

    if settings.mode == "demo":
        names = player_names(*(player.display_name if player.type == "human" else f"模拟玩家{index}"
                               for index, player in enumerate(settings.seats, 1)))

        def demo_factory():
            scripted = demo_players(scenario, analysis=tuple(p.analysis for p in settings.seats))
            return tuple(HumanPlayer(analysis=config.analysis) if config.type == "human" else player
                         for config, player in zip(settings.seats, scripted))

        with RunLogs(settings.log_dir, names=names) as logs:
            return await execute(demo_factory, names, logs)
    keys = player_keys(settings, args.secrets_file or Path("local_secrets.py"))
    names = player_names(settings.red.display_name, settings.blue.display_name)
    async with httpx.AsyncClient(timeout=policy.timeout, follow_redirects=False) as client:
        players = model_players(client, settings, keys, names)
        profile_keys = (p.get("api_key") for p in getattr(args, "_player_profiles", {}).values())
        secrets = tuple(key for key in (*keys, *(p.api_key for p in settings.seats), *profile_keys) if key)
        with RunLogs(settings.log_dir, secrets=secrets, names=names) as logs:
            return await execute(lambda: players, names, logs, client)


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
    except (KeyboardInterrupt, UserInterrupted):
        print("\nSystem：用户中止运行。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
