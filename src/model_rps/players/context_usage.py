"""观察同场上下文增长；只记录服务实际报告的 token，不推算窗口容量。"""

from dataclasses import dataclass


@dataclass
class ContextUsage:
    previous: int | None = None
    peak: int | None = None
    known_total: int = 0
    missing: int = 0

    def record(self, usage, messages: list[dict]) -> dict:
        value = usage.get("prompt_tokens", usage.get("input_tokens")) if isinstance(usage, dict) else None
        value = value if type(value) is int and value >= 0 else None
        delta = value - self.previous if value is not None and self.previous is not None else None
        if value is None:
            self.missing += 1
        else:
            self.peak = max(self.peak or 0, value)
            self.known_total += value
        self.previous = value
        return {"input_tokens": value, "input_tokens_delta": delta, "peak_input_tokens": self.peak,
                "known_input_tokens_total": self.known_total, "missing_usage_requests": self.missing,
                "message_count": len(messages), "input_characters": sum(len(m["content"]) for m in messages)}
