"""终端显示辅助；颜色仅用于展示，不写入日志或模型上下文。"""

import os
import re
import sys


def supports_color() -> bool:
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if sys.platform != "win32":
        return True
    # Windows 控制台需要启用 VT；失败时保持纯文本。
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.GetStdHandle(-11)
    mode = wintypes.DWORD()
    return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                and kernel.SetConsoleMode(handle, mode.value | 0x0004))


def color_names(text: str, names: dict[str, str], *, aliases: dict[str, str] | None = None) -> str:
    colors = {names[seat]: color for seat, color in (("A", "91"), ("B", "94")) if seat in names}
    colors.update({name: "91" if seat == "A" else "94" for name, seat in (aliases or {}).items()})
    if not colors:
        return text
    pattern = "|".join(re.escape(name) for name in sorted(colors, key=len, reverse=True))
    return re.sub(pattern, lambda match: f"\x1b[{colors[match[0]]}m{match[0]}\x1b[0m", text)
