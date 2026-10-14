"""请求诊断事件；ContextVar 让并发请求及其重试各自携带正确的标识。"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

TraceHandler = Callable[[dict], None]
_handler: ContextVar[TraceHandler | None] = ContextVar("request_trace", default=None)


class LogError(OSError):
    """日志写入失败必须停止比赛，不能作为模型错误反复重试。"""


@contextmanager
def request_trace(handler: TraceHandler | None, *, player: str, tick: int, attempt: int, phase: str = "game"):
    def emit(record: dict) -> None:
        if handler is not None:
            handler({"player": player, "tick": tick, "attempt": attempt, "phase": phase, **record})

    token = _handler.set(emit)
    try:
        yield
    finally:
        _handler.reset(token)


def trace(kind: str, **data) -> None:
    handler = _handler.get()
    if handler is not None:
        handler({"kind": kind, **data})
