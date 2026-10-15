"""将裁判内部的席位标识转换为模型名；不改写玩家生成的自然语言。"""

import hashlib
import re


def player_names(first: str, second: str) -> dict[str, str]:
    if first.casefold() == second.casefold():
        return {"A": f"{first}（玩家1）", "B": f"{second}（玩家2）"}
    return {"A": first, "B": second}


def named_data(value, names: dict[str, str], field: str | None = None):
    """转换结构化身份、比分与身份索引，不替换 SAY/说明/HTTP 原文中的文本。"""
    if isinstance(value, dict):
        return {names.get(key, key): named_data(item, names, key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if field == "wins" and len(value) == 2:
            return {names["A"]: value[0], names["B"]: value[1]}
        return [named_data(item, names) for item in value]
    if field in ("player", "winner") and isinstance(value, str):
        return names.get(value, value)
    return value


def log_directory(name: str) -> str:
    """模型标识可能含路径分隔符；目录始终是一个合法的本地文件名。"""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if not safe or safe.split(".")[0].upper() in reserved:
        safe = "model_" + safe
    if len(safe) > 100:
        safe = safe[:100]
    if safe != name:
        safe += "_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return safe
