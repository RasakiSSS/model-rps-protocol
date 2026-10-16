"""独立的只读调试窗口，读取本次运行的现有日志，不持有密钥或玩家。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unicodedata

from .terminal import color_names, supports_color


def open_debug_window(logs):
    """启动失败不影响比赛，也不将诊断内容混入前台。"""
    try:
        if sys.platform != "win32":
            raise OSError("unsupported platform")
        process = subprocess.Popen(
            [sys.executable, "-m", "model_rps.debug_console", str(logs.path.resolve()),
             "--parent", str(os.getpid()), "--window"],
            creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True,
        )
    except OSError:
        logs.lifecycle("debug_window_error", message="无法打开独立调试窗口；比赛继续，详情可从日志查看。")
        # 仅启动失败时提示诊断入口，绝不在前台输出请求细节。
        print("调试窗口未打开；比赛继续，请查看前台列出的日志目录。", file=sys.stderr)
        return None
    # 回收已关闭的查看器；等待发生在后台，不拖住比赛或程序退出。
    threading.Thread(target=process.wait, daemon=True, name="debug-window-wait").start()
    return process


class LogTail:
    """二进制缓冲确保只解析完整 UTF-8 JSON 行，包括写入中途的多字节字符。"""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.pending = b""
        self.closed = False
        self.eof = False

    def read(self) -> list[dict]:
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(65536)
        self.offset += len(data)
        self.eof = len(data) < 65536
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()
        records = []
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(row, dict):
                records.append(row)
                if row.get("kind") == "run_closed":
                    self.closed = True
        return records


def format_record(row: dict) -> str | None:
    """展示诊断概况；完整请求、思考和正文仍可在原始日志查看。"""
    kind = row.get("kind")
    if kind in {"stream_delta", "raw_response", "http_request", "http_error_body", "decision", "postmatch_response"}:
        return None
    label = row.get("player", "System")
    if "attempt" in row or "request_id" in row:
        phase = "赛后聊天" if row.get("phase") == "postmatch" else f"第 {row.get('game')} 局 Tick {row.get('tick')}"
        if row.get("phase") == "chat":
            phase += " · 聊天"
        if row.get("request_id") is not None:
            phase += f" · 响应 #{row['request_id']}"
        label += f" · {phase}"
        if "attempt" in row:
            label += f" · 第 {row['attempt']} 次请求"
    if kind == "tick":
        detail = row["text"]
    elif kind == "request_start":
        detail = "等待响应"
        reasons = row.get("state", {}).get("request_reasons", [])
        labels = {"game_start": "开局", "turn_changed": "行动轮换", "tick_started": "新 Tick",
                  "action_available": "行动机会", "action_ended": "自己的行动已结束", "opponent_say": "对手发言"}
        if reasons:
            detail += "；触发：" + "、".join(labels.get(reason, reason) for reason in reasons)
        if row.get("correction"):
            detail += "；纠错反馈：" + row["correction"]
    elif kind == "stream_status":
        detail = {"waiting": "等待响应", "thinking": "接收思考", "generating": "接收正文"}.get(row.get("status"), "接收中")
    elif kind == "request_end":
        detail = f"请求结束：{row.get('status')}；耗时 {row.get('elapsed_seconds', 0):.2f} 秒"
        if row.get("error"):
            detail += "；" + row["error"]
        if row.get("will_retry"):
            detail += "；将重试"
    elif kind == "http_response":
        detail = f"HTTP {row.get('status')}"
    elif kind == "stream_end":
        detail = f"流式接收结束：{row.get('status')}；分片 {row.get('chunks')}"
    elif kind == "reasoning":
        detail = f"思考：{row.get('status')}；token {row.get('reasoning_tokens')}"
    elif kind == "context_usage":
        current, delta = row.get("input_tokens"), row.get("input_tokens_delta")
        detail = (f"输入 token：{current if current is not None else '未提供'}；"
                  f"较上次：{format(delta, '+d') if delta is not None else '未知'}；"
                  f"本场已知累计：{row.get('known_input_tokens_total')}；"
                  f"用量缺失请求：{row.get('missing_usage_requests')}；消息数：{row.get('message_count')}")
    elif kind == "interaction_applied":
        if row.get("ended_turn"):
            detail = "已应用：结束行动"
        elif row.get("silent"):
            detail = "已应用：保持沉默"
        else:
            detail = f"已应用：公开 {len(row.get('event_sequences', []))} 条发言"
        detail += f"；应用于 Tick {row.get('applied_tick')}"
    elif kind == "postmatch_applied":
        detail = f"已公开 {len(row.get('event_sequences', []))} 条赛后事件"
        if row.get("exit"):
            detail += "；玩家退出"
    elif kind == "request_discarded":
        detail = "响应未应用：" + row.get("reason", "对局结束或取消")
    else:
        detail = f"{kind}：" + json.dumps({key: value for key, value in row.items()
                                           if key not in {"timestamp", "kind"}}, ensure_ascii=False)
    text = f"[{row.get('timestamp', '')}] {label}：{detail}"
    return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c).startswith("C") else c for c in text)


class ParentProcess:
    """保存父进程句柄，避免异常退出后窗口永久等待，或 PID 重用误判。"""

    def __init__(self, pid: int | None):
        self.pid = pid
        self.handle = None
        if pid is not None and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.OpenProcess(0x00100000, False, pid)

    def alive(self) -> bool:
        if self.pid is None:
            return True
        if sys.platform == "win32":
            return bool(self.handle) and self.kernel.WaitForSingleObject(self.handle, 0) == 258
        try:
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)


def watch(path: Path, *, parent: int | None = None, once: bool = False) -> None:
    tails = [LogTail(path / seat / "debug.log") for seat in ("red", "blue")]
    process = ParentProcess(parent)
    color = supports_color()
    names = {"A": "Red", "B": "Blue"}
    print("Model RPS · Debug（只读；关闭此窗口不会中止比赛）", flush=True)
    try:
        while True:
            batches = [tail.read() for tail in tails]
            rows = [row for index, batch in enumerate(batches) for row in batch
                    if index == 0 or "player" in row]  # 生命周期在双方日志中各有一份。
            rows.sort(key=lambda row: row.get("timestamp", ""))
            for row in rows:
                if row.get("kind") == "match_start":
                    # 生命周期中的 players 是显示名映射，字典键已转换为显示名。
                    values = list(row.get("players", {}).values())
                    if len(values) == 2:
                        names = dict(zip(("A", "B"), values))
                line = format_record(row)
                if line:
                    print(color_names(line, names) if color else line, flush=True)
            # 排空已有日志后再退出，避免一次读取上限导致尾部记录丢失。
            if not any(batches) and all(tail.eof for tail in tails):
                if once or all(tail.closed for tail in tails) or not process.alive():
                    break
                time.sleep(0.1)
        print("调试记录读取结束。", flush=True)
    finally:
        process.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读查看某次运行的调试日志")
    parser.add_argument("path", type=Path)
    parser.add_argument("--parent", type=int)
    parser.add_argument("--once", action="store_true", help="仅读取已有日志，不等待新事件")
    parser.add_argument("--window", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if args.window and sys.platform == "win32":
        # 显式绑定新控制台，避免继承父进程重定向的标准流。
        sys.stdin = open("CONIN$", "r", encoding="utf-8")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("Model RPS · Debug")
    try:
        watch(args.path, parent=args.parent, once=args.once)
    except (OSError, ValueError):
        print("调试日志无法读取；请检查日志目录。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if args.window and not args.once:
            try:
                input("按 Enter 关闭此窗口。")
            except (EOFError, KeyboardInterrupt):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
