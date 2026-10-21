"""比赛之间的本地玩家选择；控制命令不进入玩家上下文或公共聊天。"""

import argparse

from prompt_toolkit import PromptSession
from prompt_toolkit.output import create_output

from ..llm.adapters import MODEL_ADAPTERS
from .console import display_text
from .human_terminal import UserInterrupted
from .terminal import color_names, supports_color


HELP = (
    "/profiles 查看可选玩家 · /red 名称 · /blue 名称 · /swap 交换席位\n"
    "/status 查看下一场 · /start 开始下一场 · /exit 结束运行 · /help 查看帮助"
)


def select_profile(args, seat, profile):
    """完整替换一个席位，避免旧席位密钥、地址或类型覆盖新 profile。"""
    if seat not in ("red", "blue"):
        raise ValueError("请选择 Red 或 Blue 席位。")
    if profile not in getattr(args, "_player_profiles", {}):
        raise ValueError("未找到该 profile；用 /profiles 查看已配置的名称。")
    values = vars(args).copy()
    for key in values:
        if key.startswith(f"{seat}_"):
            values[key] = None
    values[f"_{seat}_stream_defaults"] = {}
    values[f"{seat}_profile"] = profile
    return argparse.Namespace(**values)


def swap_seats(args):
    """配置连同覆盖项整体交换；全局比赛规则保持不变。"""
    values = vars(args).copy()
    for key in vars(args):
        if key.startswith(("red_", "blue_")):
            seat, field = key.split("_", 1)
            peer = "blue" if seat == "red" else "red"
            values[key] = getattr(args, f"{peer}_{field}", None)
    for seat, peer in (("red", "blue"), ("blue", "red")):
        values[f"_{seat}_stream_defaults"] = getattr(args, f"_{peer}_stream_defaults", {})
        # 旧式内联配置的默认 adapter 与席位有关；交换时必须跟随玩家。
        profile = getattr(args, f"{peer}_profile", None)
        selected = getattr(args, "_player_profiles", {}).get(profile, {})
        values[f"{seat}_adapter"] = (getattr(args, f"{peer}_adapter", None) or selected.get("adapter")
                                      or ("openai" if peer == "red" else "deepseek"))
    return argparse.Namespace(**values)


def _description(selected, default_adapter="openai"):
    if selected.get("type", "llm") == "human":
        return selected.get("name") or "玩家", "human"
    adapter = selected.get("adapter", default_adapter)
    model = selected.get("model") or MODEL_ADAPTERS[adapter].model
    return selected.get("name") or model, f"llm / {adapter} / {model}"


def status_lines(args):
    """只读取可公开的名称和类型；草稿可暂时含两位人类，开赛时统一校验。"""
    for seat in ("red", "blue"):
        profile = getattr(args, f"{seat}_profile", None)
        selected = dict(getattr(args, "_player_profiles", {}).get(profile, {}))
        for field in ("type", "name", "model", "adapter"):
            value = getattr(args, f"{seat}_{field}", None)
            if value is not None:
                selected[field] = value
        name, details = _description(selected, "openai" if seat == "red" else "deepseek")
        yield f"{seat.title()} · {display_text(name)}｜{display_text(details)}｜profile: {display_text(profile) if profile else '未引用（当前席位配置）'}"


async def choose_next_match(args, *, validate, session=None, output=None):
    """只在上一场全部清理后读取指令；/start 校验通过才交还新的开赛配置。"""
    draft = argparse.Namespace(**vars(args))
    output = output or print
    session = session or PromptSession(output=create_output(), erase_when_done=True)
    color = supports_color()

    def status():
        output(f"下一场：{draft.mode} · BO{draft.bo} · {draft.initiative} · {draft.move_policy}")
        for line in status_lines(draft):
            label = line.split("｜", 1)[0]
            output(color_names(line, {"A" if line.startswith("Red") else "B": label}) if color else line)

    output("\nSystem：进入比赛大厅。此处不发送模型请求；配置只用于下一场，不写回文件。")
    output(HELP)
    status()
    while True:
        try:
            line = await session.prompt_async("下一场 > ", bottom_toolbar="/start 开始 · /profiles 玩家列表 · /exit 退出")
        except (EOFError, KeyboardInterrupt):
            raise UserInterrupted from None
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        command = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else None
        if command in ("/red", "/blue") and value:
            try:
                draft = select_profile(draft, command[1:], value)
            except ValueError as exc:
                output(str(exc))
            else:
                output("已选择完整 profile，清除该席位旧的覆盖项；全局设置继续生效。")
                status()
        elif command == "/swap" and value is None:
            draft = swap_seats(draft)
            status()
        elif command == "/profiles" and value is None:
            profiles = getattr(draft, "_player_profiles", {})
            if not profiles:
                output("未配置 profiles；可 /start 使用当前玩家再来一场，或退出后在 TOML 中添加条目。")
            for name, profile in profiles.items():
                display, details = _description(profile)
                output(f"{display_text(name)}：{display_text(display)}｜{display_text(details)}")
        elif command == "/status" and value is None:
            status()
        elif command == "/help" and value is None:
            output(HELP)
        elif command == "/start" and value is None:
            try:
                settings = validate(draft)
            except ValueError as exc:
                output(f"无法开始：{display_text(str(exc))}")
                continue
            output("System：开始新比赛；上下文、比分、草稿与日志独立重建。")
            return draft, settings
        elif command == "/exit" and value is None:
            output("System：已结束连续比赛。")
            return None
        else:
            output("无效的大厅指令；使用 /help 查看帮助。聊天与游戏指令只在比赛内使用。")
