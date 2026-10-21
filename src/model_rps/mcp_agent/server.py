"""stdio MCP 入口：自建单 Agent 比赛，或桥接到双外部玩家共享房间。"""

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
import os
import sys
from typing import Any

import httpx

from ..cli import make_parser
from ..commands import AnalysisPolicy
from ..config import CONFIG_PATH, RuntimeConfig, load_config, text_value
from ..game import MAX_TICKS, InvalidResponse
from ..match import MatchConfig
from .room import MCPRoom
from ..player_factory import model_player, player_keys
from ..players import demo_players
from ..prompts import MCP_INSTRUCTIONS, MOVE_RULES
from ..room.remote import RemoteRoom, RoomConnector
from ..runner import RequestPolicy


def create_server(room):
    # CLI 普通模式不依赖 MCP；--help 也不需要先装 SDK。
    from mcp.server import MCPServer
    from mcp_types import ToolAnnotations

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield
        finally:
            await room.close()

    server = MCPServer("model-rps", instructions=MCP_INSTRUCTIONS, lifespan=lifespan, log_level="WARNING")
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=write)
    async def rps_start(room_url: str | None = None, token: str | None = None,
                        name: str | None = None, player_id: str | None = None) -> dict[str, Any]:
        """共享入口用 room_url、token 和可选 name 加入，保存返回的 state.player_id 供之后每次调用。

        同场重复邀请幂等；刷新只传 player_id。服务端 /start 后用原邀请重新加入，获取新的 player_id。
        每个 player_id 固定绑定场次和席位，不向其他玩家提供。
        独立比赛或固定房间入口省略参数。token 决定 Red/Blue，name 仅为显示名。
        """
        try:
            if isinstance(room, RoomConnector):
                state = await room.start(room_url, token, name, player_id)
                config = room.player_room(state['player_id']).config
            else:
                if any(value is not None for value in (room_url, token, name, player_id)):
                    raise InvalidResponse('当前入口已固定比赛；动态加入请将 MCP 启动参数改为 --shared-room。')
                state = await room.start()
                config = room.config
        except InvalidResponse as exc:
            return {"error": str(exc)}
        return {"instructions": MCP_INSTRUCTIONS, "rules": {
            "best_of": config.best_of, "target_wins": config.target_wins,
            "max_games": config.max_games, "initiative": config.initiative.value,
            "move_policy": config.move_policy.value, "move_rule": MOVE_RULES[config.move_policy],
            "max_turns_per_player": MAX_TICKS,
        }, "state": state}

    @server.tool(annotations=read)
    async def rps_state(after_revision: int = 0, player_id: str | None = None) -> dict[str, Any]:
        """读取自己的状态和公共事件；不推进比赛。共享入口必须传 rps_start 返回的 player_id。"""
        try:
            if isinstance(room, RoomConnector):
                return await room.read(after_revision, player_id=player_id)
            if player_id is not None:
                raise InvalidResponse('固定比赛入口不使用 player_id。')
            return await room.read(after_revision)
        except InvalidResponse as exc:
            return {"error": str(exc)}

    @server.tool(annotations=write)
    async def rps_act(actions: list[dict[str, Any]], turn_token: str, action_id: str,
                      after_revision: int = 0, player_id: str | None = None) -> dict[str, Any]:
        """提交本席指令；共享入口必须传 player_id。action_id 去重，过期或跨席 turn_token 拒绝。"""
        try:
            if isinstance(room, RoomConnector):
                return await room.act(actions, turn_token, action_id, after_revision, player_id=player_id)
            if player_id is not None:
                raise InvalidResponse('固定比赛入口不使用 player_id。')
            return await room.act(actions, turn_token, action_id, after_revision)
        except InvalidResponse as exc:
            result = {"accepted": False, "error": str(exc)}
            try:
                if isinstance(room, RoomConnector):
                    result['state'] = await room.read(player_id=player_id)
                elif player_id is None:
                    result['state'] = await room.read()
            except InvalidResponse:
                pass
            return result

    @server.tool(annotations=read)
    async def rps_wait(after_revision: int, timeout_seconds: float = 20,
                       player_id: str | None = None) -> dict[str, Any]:
        """等待本席位事件，最多 20 秒；共享入口必须传 player_id。不提交行动，不消耗 Tick。"""
        try:
            if isinstance(room, RoomConnector):
                return await room.wait(after_revision, timeout_seconds, player_id=player_id)
            if player_id is not None:
                raise InvalidResponse('固定比赛入口不使用 player_id。')
            return await room.wait(after_revision, timeout_seconds)
        except InvalidResponse as exc:
            return {"error": str(exc)}

    return server


def make_room(argv=None):
    raw = sys.argv[1:] if argv is None else list(argv)
    if '--shared-room' in raw or any(arg == '--room-url' or arg.startswith('--room-url=') for arg in raw):
        parser = argparse.ArgumentParser(description='将本席位 MCP 工具桥接到已有共享房间。')
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--shared-room', action='store_true', help='固定宿主入口，房间邀请通过 rps_start 传入')
        mode.add_argument('--room-url', help='兼容旧用法：启动时固定房间地址')
        parser.add_argument('--token', help='固定房间的席位凭证，也可用环境变量 RPS_SEAT_TOKEN')
        parser.add_argument('--agent-name', default='Agent', help='默认显示名，共享入口可在 rps_start 中指定 name')
        args = parser.parse_args(raw)
        if args.shared_room:
            if args.token is not None:
                raise ValueError('--shared-room 的席位 token 请通过 rps_start 传入。')
            return RoomConnector(name=args.agent_name)
        return RemoteRoom(args.room_url, args.token or os.environ.get('RPS_SEAT_TOKEN'),
                          name=args.agent_name, kind='mcp')
    probe = argparse.ArgumentParser(add_help=False)
    source = probe.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path)
    source.add_argument("--no-config", action="store_true")
    config_args, _ = probe.parse_known_args(raw)
    config = {} if config_args.no_config or "--help" in raw or "-h" in raw else load_config(
        config_args.config or CONFIG_PATH, required=config_args.config is not None)
    parser = make_parser(config)
    parser.description = "Model RPS MCP：一个外部 Agent 对战模拟或 API 玩家；stdio，每进程一场 BO"
    parser.epilog = "长期使用共享房间：--shared-room，再调用 rps_start(room_url, token, name) 加入。此模式不读取模型配置；其他参数见 --shared-room --help。仍兼容 --room-url 固定房间。"
    # MCP 缺省使用离线对手，真实模型需显式 --mode live 或配置 app.mode。
    if "mode" not in config:
        parser.set_defaults(mode="demo")
    parser.add_argument("--agent-seat", choices=("Red", "Blue"), default="Red", help="外部 Agent 的固定席位，默认 Red")
    parser.add_argument("--agent-name", default="外部 Agent", help="外部 Agent 的公开显示名称")
    parser.add_argument("--agent-analysis", choices=tuple(AnalysisPolicy), default="required", help="外部 Agent 分析要求，默认 required")
    args = parser.parse_args(raw)
    if args.session or args.debug:
        raise ValueError("MCP 入口目前每进程一场 BO，不使用 --session 或 --debug；调试信息仍写日志。")
    identity, index = ("A", 0) if args.agent_seat == "Red" else ("B", 1)
    prefix = "red" if index == 0 else "blue"
    text_value(args.agent_name, "agent-name")
    # 外部输入不使用模型密钥；仅借用配置层的非 API 玩家标记，不创建人类终端。
    setattr(args, f"{prefix}_type", "human")
    setattr(args, f"{prefix}_name", args.agent_name)
    setattr(args, f"{prefix}_analysis", args.agent_analysis)
    settings = RuntimeConfig.from_args(args)
    opponent = settings.seats[1 - index]
    if opponent.type != "llm":
        raise ValueError("自建比赛的对手须为 llm；对战人类或外部 Agent 请使用 --room-url 加入共享房间。")
    match = MatchConfig(settings.best_of, settings.max_games, settings.move_policy, settings.initiative)
    scenario = args.scenario or ("locked" if match.move_policy == "locked" else "change")
    if settings.mode == "demo" and scenario in ("change", "settlement") and match.move_policy == "locked":
        raise ValueError("change 和 settlement 场景需要 --move-policy revisable。")
    key = None
    if settings.mode == "live":
        key = player_keys(settings, args.secrets_file or Path("local_secrets.py"))[1 - index]

    @asynccontextmanager
    async def open_opponent():
        if settings.mode == "demo":
            yield demo_players(scenario, analysis=tuple(p.analysis for p in settings.seats))[1 - index]
        else:
            async with httpx.AsyncClient(timeout=settings.timeout_seconds, follow_redirects=False) as client:
                yield model_player(client, settings, opponent, key, room.names)

    room = MCPRoom(open_opponent, config=match, policy=RequestPolicy(settings.timeout_seconds, settings.retry_delay_seconds),
                   identity=identity, name=args.agent_name, analysis=args.agent_analysis,
                   opponent_name=opponent.display_name if settings.mode == "live" else "模拟对手",
                   log_dir=settings.log_dir, secrets=tuple(p.api_key for p in settings.seats if p.api_key) + ((key,) if key else ()))
    return room


def main(argv=None):
    try:
        room = make_room(argv)
        server = create_server(room)
    except ImportError:
        print('请先使用项目 Python 安装 MCP 可选依赖：pip install -e ".[mcp]"。', file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
