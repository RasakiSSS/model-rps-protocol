"""裁判席位与 Red / Blue 展示身份；不改写玩家生成的自然语言。"""


def player_names(first: str, second: str) -> dict[str, str]:
    return {"A": f"Red · {first}", "B": f"Blue · {second}"}


SEAT_DIRECTORIES = {"A": "red", "B": "blue"}


def named_data(value, names: dict[str, str], field: str | None = None):
    """转换结构化身份、比分与身份索引，不替换 SAY/说明/HTTP 原文中的文本。"""
    if isinstance(value, dict):
        return {names.get(key, key): named_data(item, names, key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if field == "wins" and len(value) == 2:
            return {names["A"]: value[0], names["B"]: value[1]}
        return [named_data(item, names) for item in value]
    if field in ("player", "winner", "first_player", "failed_player") and isinstance(value, str):
        return names.get(value, value)
    return value
