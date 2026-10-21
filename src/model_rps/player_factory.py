"""配置到独立玩家实例的组装；裁判不依赖模型厂商。"""

import ast
from dataclasses import replace
from pathlib import Path

from .config import RuntimeConfig
from .players.base import ChatCompletionPlayer
from .llm.adapters import MODEL_ADAPTERS, configure_thinking
from .players.human import HumanPlayer


def load_keys(path: Path, *, required=("openai", "deepseek")) -> tuple[str | None, str | None]:
    """只读取模型席位所需的字符串常量，不执行密钥文件中的 Python 代码。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError, UnicodeError):
        raise ValueError("无法读取密钥文件；请在 config.toml 或 local_secrets.py 中填写模型席位所需的 API key。") from None
    keys = {}
    wanted = {"OPENAI_API_KEY" if adapter == "openai" else "DEEPSEEK_API_KEY" for adapter in required}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                try:
                    value = ast.literal_eval(statement.value)
                except (ValueError, TypeError, SyntaxError):
                    raise ValueError("密钥必须直接填写为字符串常量。") from None
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"请在本地密钥文件中填写 {target.id}。")
                keys[target.id] = value
    if set(keys) != wanted:
        raise ValueError("密钥文件缺少模型席位所需的 API key 字符串常量。")
    return keys.get("DEEPSEEK_API_KEY"), keys.get("OPENAI_API_KEY")


def player_keys(settings, path):
    required = [player for player in settings.seats if player.type == "llm"]
    if any(player.api_key is not None for player in required):
        if not all(player.api_key for player in required):
            raise ValueError("TOML 配置必须填写所有模型席位的 api_key；人类席位不需要密钥。")
        return tuple(player.api_key if player.type == "llm" else None for player in settings.seats)
    if any(player.adapter not in ("openai", "deepseek") for player in required):
        raise ValueError("新增模型服务请在 TOML 的席位或模型配置中填写 api_key；旧密钥文件仅支持 OpenAI / DeepSeek。")
    deepseek, openai = load_keys(path, required={player.adapter for player in required})
    legacy = {"deepseek": deepseek, "openai": openai}
    return tuple(legacy[player.adapter] if player.type == "llm" else None for player in settings.seats)



def model_players(client, settings: RuntimeConfig, keys, names):
    return tuple(model_player(client, settings, config, key, names)
                 for config, key in zip(settings.seats, keys, strict=True))


def model_player(client, settings, config, key, names):
    if config.type == "human":
        return HumanPlayer(analysis=config.analysis)
    spec = configure_thinking(replace(MODEL_ADAPTERS[config.adapter], model=config.model,
                                      base_url=config.base_url), config.thinking)
    streaming = settings.streaming_for(config)
    return ChatCompletionPlayer(
        client, spec, key, names=names, max_output_tokens=settings.max_output_tokens,
        analysis=config.analysis,
        stream=streaming["stream"], stream_first_timeout=streaming["stream_first_timeout_seconds"],
        stream_idle_timeout=streaming["stream_idle_timeout_seconds"])
