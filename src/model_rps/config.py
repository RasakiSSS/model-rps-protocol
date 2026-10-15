"""TOML 配置读取与严格校验。"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
import math
import tomllib
import unicodedata
from urllib.parse import urlsplit

from .players import DEEPSEEK_THINKING_MODES, OPENAI_THINKING_MODES


CONFIG_PATH = Path("config.toml")
_SECTIONS = {"app", "match", "players"}
_APP = {"mode", "log_dir", "timeout_seconds", "retry_delay_seconds", "max_output_tokens"}
_MATCH = {"move_policy", "best_of", "max_games", "initiative"}
_PLAYER = {"name", "model", "base_url", "api_key", "thinking"}


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

    @property
    def display_name(self) -> str:
        return self.name or self.model


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
    gpt: PlayerConfig
    deepseek: PlayerConfig

    @classmethod
    def from_args(cls, args) -> RuntimeConfig:
        choice_value(args.mode, ("live", "demo"), "mode")
        choice_value(args.move_policy, ("locked", "revisable"), "move_policy")
        choice_value(args.initiative, ("simultaneous",), "initiative（异步先后手尚未实现）")
        number_value(args.timeout, "请求超时")
        number_value(args.retry_delay, "retry_delay_seconds", zero=True)
        number_value(args.max_output_tokens, "max_output_tokens", integer=True)
        number_value(args.bo, "best_of", integer=True)
        if args.bo % 2 != 1:
            raise ValueError("best_of 必须为正奇数。")
        if args.max_games is not None:
            number_value(args.max_games, "max_games", integer=True)
            if args.max_games < args.bo // 2 + 1:
                raise ValueError("max_games 不能小于目标胜局数。")
        players = []
        for prefix, choices in (("openai", OPENAI_THINKING_MODES), ("deepseek", DEEPSEEK_THINKING_MODES)):
            model, url, thinking, name, key = (getattr(args, f"{prefix}_{key}") for key in
                                             ("model", "base_url", "thinking", "name", "api_key"))
            text_value(model, f"{prefix}.model")
            url_value(url, f"{prefix}.base_url")
            choice_value(thinking, choices, f"{prefix}.thinking")
            if name is not None:
                text_value(name, f"{prefix}.name")
                if name in ("A", "B"):
                    raise ValueError("显示名称不能使用裁判保留的 A/B 标识。")
            if model in ("A", "B") and name is None:
                raise ValueError("此模型标识需要设置独立的显示名称。")
            if key is not None:
                text_value(key, f"{prefix}.api_key", empty=args.mode == "demo")
            players.append(PlayerConfig(model, url, thinking, name, key))
        return cls(args.mode, args.timeout, args.retry_delay, args.max_output_tokens, Path(args.log_dir),
                   args.bo, args.max_games, args.move_policy, args.initiative, *players)


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
    _check_keys(app, _APP, "[app]")
    _check_keys(match, _MATCH, "[match]")
    _check_keys(players, {"gpt", "deepseek"}, "[players]")
    for name in ("gpt", "deepseek"):
        player = _table(players, name)
        _check_keys(player, _PLAYER, f"[players.{name}]")
        for key, value in player.items():
            text_value(value, f"players.{name}.{key}", empty=key == "api_key")
        if "base_url" in player:
            url_value(player["base_url"], f"players.{name}.base_url")
        if "thinking" in player:
            choice_value(player["thinking"], OPENAI_THINKING_MODES if name == "gpt" else DEEPSEEK_THINKING_MODES,
                         f"players.{name}.thinking")
    for key, value in app.items():
        if key in ("mode", "log_dir"):
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
        choice_value(match["initiative"], ("simultaneous",), "match.initiative（异步先后手尚未实现）")
    if "best_of" in match and match["best_of"] % 2 != 1:
        raise ValueError("match.best_of 必须为正奇数。")
    result = {}
    result.update({"mode": app.get("mode"), "timeout": app.get("timeout_seconds"),
                   "retry_delay": app.get("retry_delay_seconds"),
                   "max_output_tokens": app.get("max_output_tokens"),
                   "log_dir": app.get("log_dir")})
    result.update({"move_policy": match.get("move_policy"), "bo": match.get("best_of"),
                   "max_games": match.get("max_games"), "initiative": match.get("initiative")})
    if result.get("log_dir") is not None:
        # 配置内的相对路径相对于配置所在目录；CLI 路径相对于工作目录。
        result["log_dir"] = path.resolve().parent / result["log_dir"]
    for name in ("gpt", "deepseek"):
        player = _table(players, name)
        result[name] = {key: player.get(key) for key in _PLAYER}
    return {key: value for key, value in result.items() if value is not None}


def parser_defaults(config: dict) -> dict:
    """将扁平配置转换为 argparse 默认值；API key 不会出现在帮助或日志中。"""
    defaults = {}
    mapping = {"mode": "mode", "timeout": "timeout", "bo": "bo", "max_games": "max_games",
               "move_policy": "move_policy", "log_dir": "log_dir", "retry_delay": "retry_delay",
               "max_output_tokens": "max_output_tokens", "initiative": "initiative"}
    for source, target in mapping.items():
        if source in config:
            defaults[target] = config[source]
    if "gpt" in config:
        player = config["gpt"]
        for source, target in (("model", "openai_model"), ("base_url", "openai_base_url"),
                               ("thinking", "openai_thinking"), ("api_key", "openai_api_key"), ("name", "openai_name")):
            if player.get(source) is not None:
                defaults[target] = player[source]
    if "deepseek" in config:
        player = config["deepseek"]
        for source, target in (("model", "deepseek_model"), ("base_url", "deepseek_base_url"),
                               ("thinking", "deepseek_thinking"), ("api_key", "deepseek_api_key"), ("name", "deepseek_name")):
            if player.get(source) is not None:
                defaults[target] = player[source]
    return defaults
