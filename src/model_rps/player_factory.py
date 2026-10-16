"""配置到独立玩家实例的组装；裁判不依赖模型厂商。"""

import ast
from dataclasses import replace
from pathlib import Path

from .config import RuntimeConfig
from .players import ChatCompletionPlayer, MODEL_ADAPTERS, configure_thinking

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



def model_players(client, settings: RuntimeConfig, keys, names):
    players = []
    for config, key in zip(settings.seats, keys, strict=True):
        spec = configure_thinking(replace(MODEL_ADAPTERS[config.adapter], model=config.model,
                                          base_url=config.base_url), config.thinking)
        players.append(ChatCompletionPlayer(
            client, spec, key, names=names, max_output_tokens=settings.max_output_tokens,
            stream=settings.stream, stream_first_timeout=settings.stream_first_timeout_seconds,
            stream_idle_timeout=settings.stream_idle_timeout_seconds))
    return tuple(players)
