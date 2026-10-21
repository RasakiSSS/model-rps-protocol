"""HTTP、流式生命周期与协议转换；不持有比赛历史或处理玩家指令。"""

import asyncio
from contextlib import aclosing

import httpx

from .adapters import auth_headers, endpoint, request_payload
from .anthropic import MessagesStream, normalize_response, normalize_usage
from ..core.diagnostics import trace
from .streaming import CompletionStream, StreamError, sse_data


class PlayerError(Exception):
    """safe_message 仅包含本地错误概况，不回显服务器原文或密钥。"""

    def __init__(self, safe_message: str, *, retryable: bool = True) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.retryable = retryable


class ModelTransport:
    def __init__(self, client, spec, api_key, *, max_output_tokens, stream, first_timeout, idle_timeout):
        self.client, self.spec, self._api_key = client, spec, api_key
        self.max_output_tokens, self.stream = max_output_tokens, stream
        self.first_timeout, self.idle_timeout = first_timeout, idle_timeout
        self.usage = None

    async def complete(self, messages):
        self.usage = None
        payload = request_payload(self.spec, messages, self.max_output_tokens, self.stream)
        trace("request_config", model=self.spec.model, thinking_api=self.spec.thinking_api,
              requested_thinking=self.spec.thinking_mode, protocol=self.spec.protocol,
              max_output_tokens=self.max_output_tokens, stream=self.stream,
              stream_first_timeout=self.first_timeout, stream_idle_timeout=self.idle_timeout)
        trace("http_request", payload=payload)
        if self.stream:
            return await self._stream_complete(payload)
        try:
            response = await self.client.post(endpoint(self.spec), headers=auth_headers(self.spec, self._api_key), json=payload)
        except httpx.TimeoutException:
            raise TimeoutError from None
        except httpx.RequestError:
            raise PlayerError("模型服务连接失败") from None
        trace("http_response", status=response.status_code, body=response.text)
        self._check_status(response)
        try:
            data = response.json()
        except (ValueError, RecursionError):
            raise PlayerError("模型服务返回的响应结构无效") from None
        self.usage = data.get("usage") if isinstance(data, dict) else None
        if self.spec.protocol == "anthropic_messages":
            self.usage = normalize_usage(self.usage)
            try:
                data = normalize_response(data)
            except StreamError as exc:
                raise PlayerError(str(exc)) from None
        self.usage = data.get("usage") if isinstance(data, dict) else None
        return data

    @staticmethod
    def _check_status(response):
        if not response.is_success:
            retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
            raise PlayerError(f"模型服务返回 HTTP {response.status_code}", retryable=retryable)

    async def _stream_complete(self, payload):
        assembled = MessagesStream() if self.spec.protocol == "anthropic_messages" else CompletionStream()
        status = "incomplete"
        trace("stream_status", status="waiting")
        try:
            async with asyncio.timeout(self.first_timeout) as deadline:
                async with self.client.stream("POST", endpoint(self.spec), headers=auth_headers(self.spec, self._api_key),
                                              json=payload, timeout=None) as response:
                    trace("http_response", status=response.status_code, stream=True)
                    if not response.is_success:
                        await response.aread()
                        trace("http_error_body", body=response.text)
                        self._check_status(response)
                    if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
                        raise StreamError("模型服务未返回 SSE 流式响应")
                    async with aclosing(sse_data(response)) as events:
                        async for data in events:
                            before = assembled.chunks
                            assembled.feed(data)
                            if assembled.done:
                                break
                            if assembled.chunks > before:
                                deadline.reschedule(asyncio.get_running_loop().time() + self.idle_timeout)
                    result = assembled.result()
            status = "complete"
            return result
        except StreamError as exc:
            raise PlayerError(str(exc)) from None
        except TimeoutError:
            which = "首包" if not assembled.chunks else "空闲"
            raise PlayerError(f"流式{which}等待超时") from None
        except httpx.TimeoutException:
            raise TimeoutError from None
        except httpx.RequestError:
            raise PlayerError("模型流式连接中断") from None
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self.usage = assembled.usage
            trace("stream_end", status=status, chunks=assembled.chunks, message=assembled.message,
                  usage=assembled.usage, finish_reason=assembled.finish_reason, done=assembled.done)
