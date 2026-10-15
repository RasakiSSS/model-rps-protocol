"""Chat Completions SSE 解码；只组装响应，不解析或执行游戏动作。"""

import json

import httpx

from .diagnostics import trace


class StreamError(ValueError):
    """仅携带本地生成的错误概况，不能回显服务内容。"""


async def sse_data(response: httpx.Response):
    """支持网络分片、CRLF、多行 data 与注释心跳；未终止的事件不生效。"""
    lines = []
    size = 0
    first = True
    async for line in response.aiter_lines():
        if first:
            line = line.removeprefix('\ufeff')
            first = False
        if not line:
            if lines:
                yield '\n'.join(lines)
                lines, size = [], 0
            continue
        if line.startswith(':'):
            continue
        field, _, value = line.partition(':')
        if field == 'data':
            value = value.removeprefix(' ')
            lines.append(value)
            size += len(value)
            if size > 2_000_000:
                raise StreamError('流式事件过大')
    if lines:
        raise StreamError('流式响应在事件结束前中断')


class CompletionStream:
    def __init__(self):
        self.parts = {field: [] for field in ('content', 'reasoning_content', 'reasoning')}
        self.invalid_reasoning = set()
        self.finish_reason = None
        self.usage = None
        self.done = False
        self.stage = 'waiting'
        self.chunks = 0

    @property
    def message(self):
        return {field: (None if field in self.invalid_reasoning else ''.join(parts))
                for field, parts in self.parts.items()}

    def feed(self, data: str) -> None:
        if data.strip() == '[DONE]':
            if self.finish_reason != 'stop':
                raise StreamError('流式响应未正常结束')
            self.done = True
            return
        try:
            chunk = json.loads(data)
        except (ValueError, RecursionError):
            raise StreamError('流式事件不是有效 JSON') from None
        if not isinstance(chunk, dict) or 'error' in chunk:
            raise StreamError('模型服务返回流式错误或无效事件')
        choices = chunk.get('choices')
        if not isinstance(choices, list) or len(choices) > 1:
            raise StreamError('流式响应的 choices 无效')
        self.chunks += 1
        if chunk.get('usage') is not None:
            if not isinstance(chunk['usage'], dict):
                raise StreamError('流式用量字段无效')
            self.usage = chunk['usage']
        if not choices:  # include_usage 的独立尾部事件。
            if chunk.get('usage') is None:
                raise StreamError('流式响应包含无效空事件')
            return
        choice = choices[0]
        if not isinstance(choice, dict) or type(choice.get('index')) is not int or choice['index'] != 0:
            raise StreamError('流式响应的选择索引无效')
        delta = choice.get('delta')
        if not isinstance(delta, dict):
            raise StreamError('流式响应的 delta 无效')
        if self.finish_reason is not None:
            raise StreamError('模型在结束后继续发送响应')
        if delta.get('role') not in (None, 'assistant') or any(
            delta.get(field) for field in ('tool_calls', 'function_call', 'refusal', 'audio')
        ):
            raise StreamError('流式响应包含不支持的内容')
        for field in self.parts:
            value = delta.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                if field == 'content':
                    raise StreamError('流式正文不是文本')
                self.invalid_reasoning.add(field)
                continue
            if value:
                self.parts[field].append(value)
                trace('stream_delta', field=field, content=value, chunk=self.chunks)
                stage = 'generating' if field == 'content' else 'thinking'
                if stage != self.stage:
                    self.stage = stage
                    trace('stream_status', status=stage)
        finish = choice.get('finish_reason')
        if finish is not None:
            if not isinstance(finish, str):
                raise StreamError('流式结束原因无效')
            self.finish_reason = finish
            if finish != 'stop':
                raise StreamError('模型输出未完整结束或未返回文本内容')

    def result(self):
        if not self.done:
            raise StreamError('流式响应中断，未收到结束标记')
        message = self.message
        # 保留异常字段语义，不能把类型错误误认成可用思考。
        for field in self.invalid_reasoning:
            message[field] = []
        return {'choices': [{'message': message, 'finish_reason': self.finish_reason}], 'usage': self.usage}
