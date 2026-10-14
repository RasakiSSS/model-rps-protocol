"""每次运行的五份 UTF-8 日志；记录后立即 flush，私有日志不参与模型输入。"""

import json
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .diagnostics import LogError


class RunLogs:
    def __init__(self, root: Path, *, secrets: tuple[str, ...] = ()) -> None:
        self._stack = ExitStack()
        self._secrets = tuple(sorted({value.strip() for value in secrets if value.strip()}, key=len, reverse=True))
        self.path = root / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8])
        self._files = {}
        try:
            self.path.mkdir(parents=True, exist_ok=False)
            self._files["chat"] = self._open(self.path / "chat.log")
            for player in ("A", "B"):
                directory = self.path / f"player_{player.lower()}"
                directory.mkdir()
                for name in ("thought", "debug"):
                    self._files[player, name] = self._open(directory / f"{name}.log")
        except OSError:
            self._stack.close()
            raise LogError("无法创建日志目录或文件。") from None

    def _open(self, path: Path):
        return self._stack.enter_context(path.open("x", encoding="utf-8", errors="backslashreplace"))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            for representation in (secret, json.dumps(secret)[1:-1],
                                   json.dumps(secret, ensure_ascii=False)[1:-1]):
                text = text.replace(representation, "[REDACTED]")
        return text

    def _write(self, key, line: str) -> None:
        try:
            stream = self._files[key]
            stream.write(line + "\n")
            stream.flush()
        except (OSError, UnicodeError):
            raise LogError("日志写入失败，比赛已停止。") from None

    def _json(self, key, record: dict) -> None:
        def clean(value):
            if isinstance(value, str):
                return self.redact(value)
            if isinstance(value, dict):
                return {self.redact(k): clean(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [clean(item) for item in value]
            return value

        record = {"timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"), **record}
        # 先脱敏再编码，包含引号或反斜杠的密钥也不会破坏 JSON 行。
        self._write(key, json.dumps(clean(record), ensure_ascii=False))

    def chat(self, text: str, *, game: int = 0, tick: int = 0) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        for line in text.splitlines():
            self._write("chat", self.redact(f"[{timestamp}][局 {game}][Tick {tick}] {line}"))

    def request(self, game: int, record: dict) -> None:
        player = record["player"]
        entry = {"game": game, **record}
        if record["kind"] == "decision":
            self._json((player, "thought"), entry)
        else:
            self._json((player, "debug"), entry)
            if record["kind"] == "request_end" and record["status"] != "valid":
                self._json((player, "thought"), {
                    "game": game, "player": player, "tick": record["tick"],
                    "attempt": record["attempt"], "kind": "no_decision",
                    "status": record["status"], "summary": None,
                })

    def lifecycle(self, kind: str, **data) -> None:
        for player in ("A", "B"):
            self._json((player, "debug"), {"kind": kind, **data})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if not isinstance(exc, LogError):
                self.lifecycle("run_closed", status="complete" if exc_type is None else "interrupted")
                if exc_type is not None:
                    self.chat("System：运行中断，已保存此前日志。")
        finally:
            try:
                self._stack.close()
            except OSError:
                raise LogError("日志关闭失败。") from None
