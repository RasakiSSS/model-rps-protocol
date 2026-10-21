"""独立的只读调试窗口，读取本次运行的现有日志，不持有密钥或玩家。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .debug_format import format_record
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
    aliases = {}
    buffered = {}
    next_sequence = 1
    print("Model RPS · Debug（只读；关闭此窗口不会中止比赛）", flush=True)
    try:
        while True:
            batches = [tail.read() for tail in tails]
            rows = []
            for index, batch in enumerate(batches):
                for row in batch:
                    sequence = row.get("log_sequence")
                    if isinstance(sequence, int) and sequence >= next_sequence:
                        buffered[sequence] = (index, row)
                    elif sequence is None:
                        rows.append((index, row))  # 旧日志按时间尽力排序。
            rows.sort(key=lambda item: item[1].get("timestamp", ""))
            # 保留跨文件的缺口，避免一方的长记录尚未读完时先展示另一方的新事件。
            while next_sequence in buffered:
                rows.append(buffered.pop(next_sequence))
                next_sequence += 1
            drained = not any(batches) and all(tail.eof for tail in tails)
            finished = drained and (once or all(tail.closed for tail in tails) or not process.alive())
            if finished:
                # 异常终止或损坏日志可能缺行，退出前仍展示已能读取的后续记录。
                rows.extend(buffered[key] for key in sorted(buffered))
                buffered.clear()
            for index, row in rows:
                if index != 0 and "player" not in row:
                    continue  # 生命周期在双方日志中各有一份。
                if row.get("kind") == "match_start":
                    # 生命周期中的 players 是显示名映射，字典键已转换为显示名。
                    values = list(row.get("players", {}).values())
                    if len(values) == 2:
                        names = dict(zip(("A", "B"), values))
                # 日志目录代表固定席位，换人后的名字和旧作者都沿用该席位颜色。
                if row.get("player"):
                    for name in (row["player"], row.get("actor"), row.get("previous_player")):
                        if isinstance(name, str):
                            aliases[name] = "A" if index == 0 else "B"
                line = format_record(row)
                if line:
                    print(color_names(line, names, aliases=aliases) if color else line, flush=True)
            # 排空已有日志后再退出，避免一次读取上限导致尾部记录丢失。
            if finished:
                break
            if drained:
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
