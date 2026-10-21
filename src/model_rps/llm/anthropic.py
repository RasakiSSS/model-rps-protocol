"""Anthropic Messages 文本/思考与 SSE 到统一响应的转换；不执行玩家指令。"""

import json

from ..core.diagnostics import trace
from .streaming import StreamError


def normalize_usage(usage):
    if not isinstance(usage, dict):
        return usage
    result = dict(usage)
    fields = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    if "input_tokens" in usage and all(type(usage.get(k, 0)) is int and usage.get(k, 0) >= 0 for k in fields):
        result["prompt_tokens"] = sum(usage.get(k, 0) for k in fields)
    elif "input_tokens" in usage:
        result["prompt_tokens"] = None
    return result


def completion(message, usage, stop_reason):
    return {"choices": [{"message": message, "finish_reason": "stop" if stop_reason == "end_turn" else stop_reason}],
            "usage": normalize_usage(usage)}


def normalize_response(data):
    if not isinstance(data, dict) or data.get("type") != "message" or data.get("role") != "assistant":
        raise StreamError("Messages 响应结构无效")
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise StreamError("Messages 响应内容无效")
    parts = {"content": [], "reasoning_content": []}
    for block in blocks:
        if not isinstance(block, dict):
            raise StreamError("Messages 内容块无效")
        kind = block.get("type")
        if kind == "redacted_thinking":
            continue  # 不把加密数据当作可读思考，也不回填历史。
        field, source = ("content", "text") if kind == "text" else ("reasoning_content", "thinking")
        if kind not in ("text", "thinking") or not isinstance(block.get(source), str):
            raise StreamError("Messages 包含不支持的内容块")
        parts[field].append(block[source])
    return completion({field: "".join(text) for field, text in parts.items()}, data.get("usage"), data.get("stop_reason"))


class MessagesStream:
    def __init__(self):
        self.parts = {"content": [], "reasoning_content": []}
        self.usage = None
        self.finish_reason = None
        self.done = False
        self.chunks = 0
        self.stage = "waiting"
        self.started = False
        self.block = None
        self.block_count = 0
        self.stopped_content = False

    @property
    def message(self):
        return {field: "".join(parts) for field, parts in self.parts.items()}

    def _append(self, field, value):
        if not isinstance(value, str):
            raise StreamError("Messages 流式文本字段无效")
        if value:
            self.parts[field].append(value)
            trace("stream_delta", field=field, content=value, chunk=self.chunks)
            stage = "generating" if field == "content" else "thinking"
            if stage != self.stage:
                self.stage = stage
                trace("stream_status", status=stage)

    def _usage(self, value):
        if value is not None:
            if not isinstance(value, dict):
                raise StreamError("Messages 流式用量字段无效")
            self.usage = normalize_usage({**(self.usage or {}), **value})

    def feed(self, raw):
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise StreamError("Messages 流式事件不是有效 JSON") from None
        if not isinstance(data, dict) or data.get("type") == "error":
            raise StreamError("Messages 返回流式错误或无效事件")
        kind = data.get("type")
        if kind == "ping":
            return  # 心跳不能延长首包或空闲时限。
        if self.done:
            raise StreamError("Messages 在结束后继续发送内容")
        self.chunks += 1
        if kind == "message_start":
            message = data.get("message")
            if self.started or not isinstance(message, dict) or message.get("role") != "assistant" or message.get("content") != []:
                raise StreamError("Messages 流式开始事件无效")
            self.started = True
            self._usage(message.get("usage"))
            return
        if not self.started:
            raise StreamError("Messages 缺少流式开始事件")
        if kind == "content_block_start":
            block = data.get("content_block")
            if self.block is not None or self.stopped_content or type(data.get("index")) is not int or data["index"] != self.block_count:
                raise StreamError("Messages 内容块顺序无效")
            if not isinstance(block, dict) or block.get("type") not in ("text", "thinking", "redacted_thinking"):
                raise StreamError("Messages 包含不支持的内容块")
            self.block = block["type"]
            if self.block != "redacted_thinking":
                self._append("content" if self.block == "text" else "reasoning_content",
                             block.get("text" if self.block == "text" else "thinking"))
        elif kind in ("content_block_delta", "content_block_stop"):
            if self.block is None or type(data.get("index")) is not int or data["index"] != self.block_count:
                raise StreamError("Messages 内容块索引无效")
            if kind == "content_block_stop":
                self.block = None
                self.block_count += 1
                return
            delta = data.get("delta")
            if not isinstance(delta, dict):
                raise StreamError("Messages 流式增量无效")
            if self.block == "thinking" and delta.get("type") == "signature_delta":
                return
            expected = "text_delta" if self.block == "text" else "thinking_delta"
            if self.block == "redacted_thinking" or delta.get("type") != expected:
                raise StreamError("Messages 流式增量类型无效")
            self._append("content" if self.block == "text" else "reasoning_content",
                         delta.get("text" if self.block == "text" else "thinking"))
        elif kind == "message_delta":
            if self.block is not None or not isinstance(data.get("delta"), dict):
                raise StreamError("Messages 结束状态无效")
            self.stopped_content = True
            self._usage(data.get("usage"))
            stop = data["delta"].get("stop_reason")
            if stop is not None:
                self.finish_reason = stop
                if stop != "end_turn":
                    raise StreamError("模型输出未完整结束或未返回文本内容")
        elif kind == "message_stop":
            if self.block is not None or self.finish_reason != "end_turn":
                raise StreamError("Messages 流式响应未正常结束")
            self.done = True
        else:
            raise StreamError("Messages 返回不支持的流式事件")

    def result(self):
        if not self.done:
            raise StreamError("Messages 流式响应中断，未收到结束标记")
        return completion(self.message, self.usage, self.finish_reason)
