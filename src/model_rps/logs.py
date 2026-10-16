"""每次运行的五份 UTF-8 日志；记录后立即 flush，私有日志不参与模型输入。"""

import json
import unicodedata
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .diagnostics import LogError
from .identities import SEAT_DIRECTORIES, named_data


class RunLogs:
    def __init__(self, root: Path, *, secrets: tuple[str, ...] = (), names: dict[str, str] | None = None) -> None:
        self._stack = ExitStack()
        self._secrets = tuple(sorted({value.strip() for value in secrets if value.strip()}, key=len, reverse=True))
        self.path = root / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8])
        self._files = {}
        self._stream_tails = {}
        self._stream_layout = {}
        self._secret_forms = tuple(sorted({form for secret in self._secrets for form in
                                          (secret, json.dumps(secret)[1:-1],
                                           json.dumps(secret, ensure_ascii=False)[1:-1])}, key=len, reverse=True))
        self.names = dict(names) if names is not None else None
        try:
            self.path.mkdir(parents=True, exist_ok=False)
            self._files["chat"] = self._open(self.path / "chat.log")
            for player in ("A", "B"):
                directory = self.path / SEAT_DIRECTORIES[player]
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
        self._fragment(key, line + '\n')

    def _fragment(self, key, text: str) -> None:
        try:
            stream = self._files[key]
            stream.write(text)
            stream.flush()
        except (OSError, UnicodeError):
            raise LogError("日志写入失败，比赛已停止。") from None

    def redact_value(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {self.redact(k): self.redact_value(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.redact_value(item) for item in value]
        return value

    def _json(self, key, record: dict) -> None:
        record = {"timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"), **record}
        # 先脱敏再编码，包含引号或反斜杠的密钥也不会破坏 JSON 行。
        if self.names:
            record = named_data(record, self.names)
        self._write(key, json.dumps(self.redact_value(record), ensure_ascii=False))

    def chat(self, text: str, *, game: int = 0, tick: int = 0) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        for line in text.splitlines():
            self._write("chat", self.redact(f"[{timestamp}][局 {game}][Tick {tick}] {line}"))

    def _thought(self, player: str, record: dict) -> None:
        """人类可读的私有记录；完整事件另存 debug.log，正文缩进避免伪造标题。"""
        record = self.redact_value(named_data(record, self.names) if self.names else record)

        def readable(value) -> str:
            text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
            return "".join(f"\\u{ord(char):04x}" if char != "\n" and unicodedata.category(char).startswith("C")
                           else char for char in text)

        def inline(value) -> str:
            return readable(value).replace("\n", "\\n")

        def paragraph(label: str, value) -> str:
            return label + "\n" + "\n".join("    " + line for line in readable(value).split("\n"))

        kind = record["kind"]
        phase = "赛后复盘" if record.get("phase") == "review" else f"第 {record['game']} 局 · Tick {record['tick']}"
        title = {"reasoning": "接口思考", "decision": "策略分析与行动", "review": "复盘结果",
                 "request_end": "请求未完成"}[kind]
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        lines = ["═" * 60, f"{phase} · 第 {record['attempt']} 次请求 · {title}",
                 f"模型：{inline(record['player'])}  |  时间：{timestamp}"]
        if kind == "reasoning":
            status = {"returned": "已返回思考文本", "not_returned": "接口未返回思考文本",
                      "invalid_field": "接口思考字段格式无效"}.get(record.get("status"), record.get("status"))
            tokens = record.get("reasoning_tokens")
            lines.append(f"状态：{inline(status)}  |  思考等级：{inline(record.get('requested_thinking', '未提供'))}")
            lines.append(f"思考 token：{tokens if tokens is not None else '未提供'}  |  结束原因：{inline(record.get('finish_reason') or '未提供')}")
            lines.append("可见性：仅私有日志；不进入后续请求或赛后公开记录。")
            if record.get('streamed') and record.get('content'):
                lines.append('思考内容：见上方逐步写入的流式记录。')
            elif record.get("content"):
                lines.append(paragraph("思考内容：", record["content"]))
            lines.append("注：收到思考文本不代表最终动作或复盘通过校验。")
        elif kind == "decision":
            lines.append("状态：通过校验；动作是否执行以 chat.log 的裁判记录为准。")
            source = "ANALYZE" if record.get("summary") is not None else "未提交"
            lines.append(f"分析来源：{source}")
            if record.get("summary") is not None:
                lines.append(paragraph("正文策略分析：", record["summary"]))
            else:
                lines.append("正文策略分析：未提交。接口思考独立记录，不替代玩家分析。")
            lines.append("拟执行动作：")
            gestures = {"Rock": "石头", "Paper": "布", "Scissors": "剪刀"}
            for index, action in enumerate(record.get("actions", ()), 1):
                if action["type"] == "SAY":
                    lines.append(paragraph(f"  {index}. SAY · 发言：", action["text"]))
                elif action["type"] == "MOVE":
                    lines.append(f"  {index}. MOVE · 出拳：{gestures[action['move']]}（{action['move']}）")
                else:
                    lines.append(f"  {index}. ENDTURN · 未出拳，结束本次行动")
            for action in record.get("ignored_actions", ()):
                lines.append(f"忽略的 MOVE：{gestures[action['move']]}（{action['move']}）；原拳型已锁定，不作修改。")
        elif kind == "review":
            lines.append("状态：复盘完成")
            lines.append(paragraph("复盘：", record["review"]["text"]))
        else:
            status = "已取消" if record.get("status") == "cancelled" else "请求失败或响应无效"
            lines.append(f"状态：{status}；本次未取得有效{'复盘' if record.get('phase') == 'review' else '动作'}。")
            if record.get("error"):
                lines.append(paragraph("原因：", record["error"]))
            if record.get("correction"):
                lines.append(paragraph("纠错反馈：", record["correction"]))
            if "will_retry" in record:
                lines.append("后续：将重试。" if record["will_retry"] else "后续：不再重试。")
        self._write((player, "thought"), "\n".join(lines) + "\n")

    def request(self, game: int, record: dict) -> None:
        player = record["player"]
        entry = {"game": game, **record}
        if record['kind'] == 'stream_delta':
            self._stream_delta(entry)
            return
        if record['kind'] in ('stream_end', 'request_end'):
            self._finish_stream(entry)
        self._json((player, "debug"), entry)
        if record['kind'] == 'stream_end':
            status = '接收完整；仍须通过 JSON 与动作/复盘校验。' if record['status'] == 'complete' else '接收未完成；本次片段不作为有效动作或复盘。'
            self._write((player, 'thought'), f'流式接收结束：{status}\n')
        if record["kind"] in ("reasoning", "decision", "review") or (
            record["kind"] == "request_end" and record["status"] != "valid"
        ):
            self._thought(player, entry)

    @staticmethod
    def _stream_key(entry):
        return (entry['game'], entry['player'], entry.get('phase', 'game'), entry['tick'], entry['attempt'])

    def _stream_delta(self, entry):
        key = (*self._stream_key(entry), entry['field'])
        text = self._stream_tails.pop(key, '') + entry['content']
        # 从左到右消耗完整密钥，保留可能跨分片的密钥前缀，不能逐片直接 redact。
        safe, index = [], 0
        while index < len(text):
            match = next((secret for secret in self._secret_forms if text.startswith(secret, index)), None)
            if match:
                safe.append('[REDACTED]')
                index += len(match)
            elif any(secret.startswith(text[index:]) for secret in self._secret_forms
                     if len(secret) > len(text) - index):
                break
            else:
                safe.append(text[index])
                index += 1
        if index < len(text):
            self._stream_tails[key] = text[index:]
        self._write_delta({**entry, 'content': ''.join(safe)})

    def _write_delta(self, entry):
        self._json((entry['player'], 'debug'), entry)
        if entry['field'] == 'content' or not entry['content']:
            return
        player = entry['player']
        layout = self._stream_layout.get(player)
        identity = (*self._stream_key(entry), entry['field'])
        if layout is None or layout['identity'] != identity:
            if layout is not None:
                self._fragment((player, 'thought'), '\n')
            phase = '赛后复盘' if entry.get('phase') == 'review' else f"第 {entry['game']} 局 · Tick {entry['tick']}"
            timestamp = datetime.now().astimezone().isoformat(timespec='seconds')
            name = self.redact(self.names[player] if self.names else player)
            name = ''.join(f'\\u{ord(c):04x}' if unicodedata.category(c).startswith('C') else c for c in name)
            self._write((player, 'thought'), '═' * 60 + '\n' +
                        f"{phase} · 第 {entry['attempt']} 次请求 · 流式接口思考（{entry['field']}）\n模型：{name}  |  时间：{timestamp}\n"
                        '仅私有日志；接收中，尚未校验。\n思考内容：')
            layout = {'identity': identity, 'line_start': True, 'cr': False}
            self._stream_layout[player] = layout
        output = []
        for char in entry['content']:
            if char == '\n' and layout['cr']:
                layout['cr'] = False
                continue
            layout['cr'] = char == '\r'
            if char == '\r':
                char = '\n'
            if layout['line_start']:
                output.append('    ')
            output.append(char if char == '\n' or not unicodedata.category(char).startswith('C')
                          else '    ' if char == '\t' else f'\\u{ord(char):04x}')
            layout['line_start'] = char == '\n'
        self._fragment((player, 'thought'), ''.join(output))

    def _finish_stream(self, entry):
        prefix = self._stream_key(entry)
        for key in list(self._stream_tails):
            if key[:-1] == prefix:
                self._stream_tails.pop(key)
                self._write_delta({**entry, 'kind': 'stream_delta', 'field': key[-1],
                                   'content': '[REDACTED_PREFIX]', 'buffer_flush': True})
        if entry['player'] in self._stream_layout:
            self._fragment((entry['player'], 'thought'), '\n\n')
            self._stream_layout.pop(entry['player'])

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
