"""TOML 配置读取与严格校验。"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
import math
import tomllib
import unicodedata
from urllib.parse import urlsplit

from .adapters import THINKING_MODES, MODEL_ADAPTERS
from .commands import AnalysisPolicy


CONFIG_PATH = Path("config.toml")
_SECTIONS = {"app", "match", "players", "models", "profiles"}
_STREAM_ARGS = {"stream": "stream", "stream_first_timeout_seconds": "stream_first_timeout",
                "stream_idle_timeout_seconds": "stream_idle_timeout"}
_STREAM_DEFAULTS = {"stream": False, "stream_first_timeout_seconds": 60, "stream_idle_timeout_seconds": 30}
_APP = {"mode", "log_dir", "timeout_seconds", "retry_delay_seconds", "max_output_tokens",
        "stream", "stream_first_timeout_seconds", "stream_idle_timeout_seconds"}
_MATCH = {"move_policy", "best_of", "max_games", "initiative"}
_PLAYER = {"type", "adapter", "name", "model", "base_url", "api_key", "thinking", "analysis", "profile"} | _STREAM_ARGS.keys()
_MODEL = _PLAYER - {"type", "profile"}
_PROFILE = _PLAYER - {"profile"}
_HUMAN_PROFILE = {"type", "name", "analysis"}


def text_value(value, location: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()) or any(
        unicodedata.category(char).startswith("C") for char in value
    ):
        raise ValueError(f"{location} 必须是有效的单行字符串。")


def number_value(value, location: str, *, integer: bool = False, zero: bool = False) -> None:
    valid_type = type(value) is int if integer else type(value) in (int, float)
    try:
        valid = valid_type and math.isfinite(value) and (value >= 0 if zero else value > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{location} 必须是{'非负' if zero else '正'}{'整数' if integer else '有限数值'}。")


def stream_value(key, value, location):
    if key == "stream":
        if type(value) is not bool:
            raise ValueError(f"{location} 必须为布尔值。")
    else:
        number_value(value, location)


def choice_value(value, choices, location: str) -> None:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{location} 必须是 {' / '.join(choices)}。")


def url_value(value, location: str) -> None:
    text_value(value, location)
    try:
        url = urlsplit(value)
        _ = url.port  # 触发端口格式与范围校验。
        valid = (url.scheme in ("http", "https") and url.hostname and url.username is None
                 and url.password is None and not url.query and not url.fragment and not any(c.isspace() for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{location} 必须是有效的 HTTP(S) 基础地址，不得包含鉴权、查询或片段。")


@dataclass(frozen=True)
class PlayerConfig:
    model: str
    base_url: str
    thinking: str
    name: str | None = None
    api_key: str | None = field(default=None, repr=False)
    adapter: str = "openai"
    analysis: str = "required"
    type: str = "llm"
    stream: bool | None = None
    stream_first_timeout_seconds: float | None = None
    stream_idle_timeout_seconds: float | None = None

    @property
    def display_name(self) -> str:
        return self.name or ("玩家" if self.type == "human" else self.model)


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    timeout_seconds: float
    retry_delay_seconds: float
    max_output_tokens: int
    log_dir: Path
    best_of: int
    max_games: int | None
    move_policy: str
    initiative: str
    red: PlayerConfig
    blue: PlayerConfig
    stream: bool = False
    stream_first_timeout_seconds: float = 60
    stream_idle_timeout_seconds: float = 30

    @property
    def seats(self) -> tuple[PlayerConfig, PlayerConfig]:
        return self.red, self.blue

    def streaming_for(self, player: PlayerConfig) -> dict:
        """返回该席位实际使用的流式设置；未覆盖的字段继承 app 默认值。"""
        values = {key: getattr(self, key) if getattr(player, key) is None else getattr(player, key)
                  for key in _STREAM_ARGS}
        if player.type == "human":
            values["stream"] = False
        return values

    @classmethod
    def from_args(cls, args) -> RuntimeConfig:
        choice_value(args.mode, ("live", "demo"), "mode")
        choice_value(args.move_policy, ("locked", "revisable"), "move_policy")
        choice_value(args.initiative, ("simultaneous", "alternating"), "initiative")
        if args.initiative == "alternating" and args.move_policy != "locked":
            raise ValueError("实验性 alternating 模式仅支持 locked 出拳规则。")
        number_value(args.timeout, "请求超时")
        number_value(args.retry_delay, "retry_delay_seconds", zero=True)
        number_value(args.max_output_tokens, "max_output_tokens", integer=True)
        stream_defaults = dict(_STREAM_DEFAULTS)
        stream_defaults.update(getattr(args, "_stream_defaults", {}))
        for key, argument in _STREAM_ARGS.items():
            if getattr(args, argument, None) is not None:
                stream_defaults[key] = getattr(args, argument)
            stream_value(key, stream_defaults[key], key)
        number_value(args.bo, "best_of", integer=True)
        if args.bo % 2 != 1:
            raise ValueError("best_of 必须为正奇数。")
        if args.max_games is not None:
            number_value(args.max_games, "max_games", integer=True)
            if args.max_games < args.bo // 2 + 1:
                raise ValueError("max_games 不能小于目标胜局数。")
        players = []
        for prefix in ("red", "blue"):
            profile = getattr(args, f"{prefix}_profile", None)
            profiles = getattr(args, "_player_profiles", {})
            if profile is not None and profile not in profiles:
                raise ValueError(f"{prefix}.profile 未在 [profiles] 或兼容的 [models] 中定义。")
            selected = dict(profiles.get(profile, {}))
            selected.update({key: getattr(args, f"{prefix}_{key}", None) for key in _PLAYER
                             if key not in {"profile", *_STREAM_ARGS} and getattr(args, f"{prefix}_{key}", None) is not None})
            selected.update(getattr(args, f"_{prefix}_stream_defaults", {}))
            for key, argument in _STREAM_ARGS.items():
                # 显式 CLI 高于配置；席位 CLI 又高于全局 CLI。False 也是显式覆盖。
                for cli_name in (argument, f"{prefix}_{argument}"):
                    value = getattr(args, cli_name, None)
                    if value is not None:
                        selected[key] = value
                if key in selected:
                    stream_value(key, selected[key], f"{prefix}.{key}")
            if args.secrets_file is not None:
                selected["api_key"] = None
            kind = selected.get("type", "llm")
            choice_value(kind, ("llm", "human"), f"{prefix}.type")
            adapter = selected.get("adapter", "openai" if prefix == "red" else "deepseek")
            choice_value(adapter, MODEL_ADAPTERS, f"{prefix}.adapter")
            spec = MODEL_ADAPTERS[adapter]
            choices = THINKING_MODES[adapter]
            model, url, thinking, name, key = (selected.get(key) for key in
                                              ("model", "base_url", "thinking", "name", "api_key"))
            model = spec.model if model is None else model
            url = spec.base_url if url is None else url
            thinking = spec.thinking_mode if thinking is None else thinking
            text_value(model, f"{prefix}.model")
            url_value(url, f"{prefix}.base_url")
            choice_value(thinking, choices, f"{prefix}.thinking")
            if name is not None:
                text_value(name, f"{prefix}.name")
                if name in ("A", "B"):
                    raise ValueError("显示名称不能使用裁判保留的 A/B 标识。")
            if model in ("A", "B") and name is None:
                raise ValueError("此模型标识需要设置独立的显示名称。")
            if key is not None and kind != "human":
                text_value(key, f"{prefix}.api_key", empty=args.mode == "demo")
            analysis = selected.get("analysis") or ("optional" if kind == "human" else "required")
            choice_value(analysis, tuple(AnalysisPolicy), f"{prefix}.analysis")
            players.append(PlayerConfig(model, url, thinking, name, key, adapter, analysis, kind,
                                        **{key: selected.get(key) for key in _STREAM_ARGS}))
        if sum(player.type == "human" for player in players) > 1:
            raise ValueError("一个终端目前只支持一位人类玩家，请将另一席位设为 llm。")
        return cls(args.mode, args.timeout, args.retry_delay, args.max_output_tokens, Path(args.log_dir),
                   args.bo, args.max_games, args.move_policy, args.initiative, *players,
                   **stream_defaults)


def _table(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"配置节 [{name}] 必须是表格。")
    return value


def _check_keys(data: dict, allowed: set[str], location: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        # 不回显任意未知键；错误配置可能把密钥误填到了键名中。
        raise ValueError(f"{location} 包含未知配置项，请对照 config.example.toml 检查拼写。")


def _validate_player_fields(player, allowed, location, default_adapter):
    _check_keys(player, allowed, location)
    for key, value in player.items():
        if key in _STREAM_ARGS:
            stream_value(key, value, f"{location}.{key}")
        else:
            text_value(value, f"{location}.{key}", empty=key == "api_key")
    if "base_url" in player:
        url_value(player["base_url"], f"{location}.base_url")
    adapter = player.get("adapter", default_adapter)
    if adapter is not None:
        choice_value(adapter, MODEL_ADAPTERS, f"{location}.adapter")
    if "type" in player:
        choice_value(player["type"], ("llm", "human"), f"{location}.type")
    if "analysis" in player:
        choice_value(player["analysis"], tuple(AnalysisPolicy), f"{location}.analysis")
    if "thinking" in player:
        choice_value(player["thinking"], THINKING_MODES[adapter], f"{location}.thinking")


def load_config(path: Path = CONFIG_PATH, *, required: bool = False) -> dict:
    """读取可选的 config.toml；不存在时返回空配置，避免破坏旧命令。"""
    path = Path(path)
    if not path.exists():
        if required:
            raise ValueError("指定的 TOML 配置文件不存在。")
        return {}
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("无法读取 TOML 配置；请检查文件权限、UTF-8 编码及 TOML 语法。") from None
    if not isinstance(data, dict):
        raise ValueError("TOML 配置根节点必须是表格。")
    _check_keys(data, _SECTIONS, "配置根节点")
    app = _table(data, "app")
    match = _table(data, "match")
    players = _table(data, "players")
    models = _table(data, "models")
    profiles = _table(data, "profiles")
    _check_keys(app, _APP, "[app]")
    _check_keys(match, _MATCH, "[match]")
    _check_keys(players, {"red", "blue", "gpt", "deepseek"}, "[players]")
    if set(players) & {"red", "blue"} and set(players) & {"gpt", "deepseek"}:
        raise ValueError("请统一使用 players.red/blue；不能与旧版 players.gpt/deepseek 混用。")
    legacy = bool(set(players) & {"gpt", "deepseek"})
    for profile in models.values():
        if not isinstance(profile, dict) or "adapter" not in profile:
            raise ValueError("每个 [models.名称] 必须是模型配置表，并填写 adapter。")
        _validate_player_fields(profile, _MODEL, "模型配置", None)
    for profile in profiles.values():
        if not isinstance(profile, dict):
            raise ValueError("每个 [profiles.名称] 必须是玩家配置表。")
        kind = profile.get("type", "llm")
        choice_value(kind, ("llm", "human"), "profile.type")
        if kind == "llm" and "adapter" not in profile:
            raise ValueError("模型 profile 必须填写 adapter。")
        _validate_player_fields(profile, _HUMAN_PROFILE if kind == "human" else _PROFILE, "玩家配置", None)
    if models.keys() & profiles.keys():
        raise ValueError("[models] 与 [profiles] 不能定义同名条目，请使用唯一的 profile 名称。")
    profiles = {**models, **profiles}
    for name in players:
        player = _table(players, name)
        profile = player.get("profile")
        if profile is not None:
            text_value(profile, f"players.{name}.profile")
            if profile not in profiles:
                raise ValueError(f"players.{name}.profile 未在 [profiles] 或兼容的 [models] 中定义。")
        default = profiles.get(profile, {}).get("adapter", "openai" if name in ("red", "gpt") else "deepseek")
        _validate_player_fields(player, _PLAYER, f"players.{name}", default)
    for key, value in app.items():
        if key == 'stream':
            if type(value) is not bool:
                raise ValueError("app.stream 必须为布尔值。")
        elif key in ("mode", "log_dir"):
            text_value(value, f"app.{key}")
        else:
            number_value(value, f"app.{key}", integer=key == "max_output_tokens", zero=key == "retry_delay_seconds")
    for key, value in match.items():
        if key in ("best_of", "max_games"):
            number_value(value, f"match.{key}", integer=True)
        else:
            text_value(value, f"match.{key}")
    if "mode" in app:
        choice_value(app["mode"], ("live", "demo"), "app.mode")
    if "move_policy" in match:
        choice_value(match["move_policy"], ("locked", "revisable"), "match.move_policy")
    if "initiative" in match:
        choice_value(match["initiative"], ("simultaneous", "alternating"), "match.initiative")
    if "best_of" in match and match["best_of"] % 2 != 1:
        raise ValueError("match.best_of 必须为正奇数。")
    result = {}
    if profiles:
        result["profiles"] = profiles
    result.update({"mode": app.get("mode"), "timeout": app.get("timeout_seconds"),
                   "stream": app.get("stream"),
                   "stream_first_timeout": app.get("stream_first_timeout_seconds"),
                   "stream_idle_timeout": app.get("stream_idle_timeout_seconds"),
                   "retry_delay": app.get("retry_delay_seconds"),
                   "max_output_tokens": app.get("max_output_tokens"),
                   "log_dir": app.get("log_dir")})
    result.update({"move_policy": match.get("move_policy"), "bo": match.get("best_of"),
                   "max_games": match.get("max_games"), "initiative": match.get("initiative")})
    if result.get("log_dir") is not None:
        # 配置内的相对路径相对于配置所在目录；CLI 路径相对于工作目录。
        result["log_dir"] = path.resolve().parent / result["log_dir"]
    for seat, old in (("red", "gpt"), ("blue", "deepseek")):
        player = _table(players, old if legacy else seat)
        result[seat] = {key: player.get(key) for key in _PLAYER}
    return {key: value for key, value in result.items() if value is not None}


def parser_defaults(config: dict) -> dict:
    """将扁平配置转换为 argparse 默认值；API key 不会出现在帮助或日志中。"""
    defaults = {}
    if "profiles" in config:
        defaults["_player_profiles"] = config["profiles"]
    mapping = {"mode": "mode", "timeout": "timeout", "bo": "bo", "max_games": "max_games",
               "move_policy": "move_policy", "log_dir": "log_dir", "retry_delay": "retry_delay",
               "max_output_tokens": "max_output_tokens", "initiative": "initiative"}
    for source, target in mapping.items():
        if source in config:
            defaults[target] = config[source]
    defaults["_stream_defaults"] = {key: config[argument] for key, argument in _STREAM_ARGS.items()
                                    if argument in config}
    for seat in ("red", "blue"):
        defaults[f"_{seat}_stream_defaults"] = {key: value for key, value in config.get(seat, {}).items()
                                                if key in _STREAM_ARGS and value is not None}
        for key, value in config.get(seat, {}).items():
            if value is not None and key not in _STREAM_ARGS:
                defaults[f"{seat}_{key}"] = value
    return defaults
