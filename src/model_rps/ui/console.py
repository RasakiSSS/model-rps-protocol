"""玩家前台展示；不输出请求诊断。"""

import unicodedata

from ..core.game import EndReason, Event, GameState, MAX_TICKS
from ..core.logs import RunLogs
from .terminal import color_names, supports_color

GESTURE_NAMES = {"Rock": "石头", "Paper": "布", "Scissors": "剪刀"}


def display_text(text: str) -> str:
    """将控制字符转义，确保玩家发言不会清屏或伪造新的系统消息行。"""
    return "".join(f"\\u{ord(char):04x}" if unicodedata.category(char).startswith("C") else char
                   for char in text)


class Console:
    def __init__(self, names: dict[str, str], logs: RunLogs | None = None, *, color: bool | None = None) -> None:
        self.names = {key: display_text(value) for key, value in names.items()}
        self.previous_names = {}
        self.logs = logs
        self.game = 0
        self.tick = 0
        self.color = supports_color() if color is None else color
        self.output = lambda text: print(text, flush=True)

    def rename_player(self, identity, name):
        self.previous_names[self.names[identity]] = identity
        self.names[identity] = display_text(name)

    def private(self, text: str) -> None:
        """人类自己的草稿、输入反馈与状态，不发送对手或写入 chat.log。"""
        text = display_text(text)
        if self.logs is not None:
            text = self.logs.redact(text)
        self.output(f"仅自己：{text}")

    def write(self, text: str, *, debug_only: bool = False) -> None:
        if self.logs is not None:
            text = self.logs.redact(text)
            self.logs.chat(text, game=self.game, tick=self.tick)
        if debug_only:
            if self.logs is not None:
                self.logs.lifecycle("tick", game=self.game, tick=self.tick, text=text.strip())
        else:
            self.output(color_names(text, self.names, aliases=self.previous_names) if self.color else text)

    def event(self, event: Event) -> None:
        self.tick = event.tick
        name = display_text(event.actor) if event.actor else self.names.get(event.player, "System")
        if event.kind == "tick":
            if event.text == "settlement":
                self.write(f"\n── 结算 Tick {event.tick}（最后一次行动机会）──", debug_only=True)
            else:
                self.write(f"\n── Tick {event.tick}/{MAX_TICKS} ──", debug_only=True)
        elif event.kind == "turn":
            self.write(f"\n── 行动 Tick {event.tick}/{MAX_TICKS * 2} · {name} 第 {event.text}/{MAX_TICKS} 次行动 ──", debug_only=True)
        elif event.kind == "turn_reminder":
            self.write(f"System：{name}，{event.text}")
        elif event.kind == "move_ignored":
            self.write(f"System：{name} 出拳已锁定，本次 MOVE 无效；保留原拳型。")
        elif event.kind == "say":
            self.write(f"{name}：{display_text(event.text)}")
        elif event.kind in ("commit", "revise"):
            action = "已提交出拳" if event.kind == "commit" else "已修改出拳"
            self.write(f"System：{name} {action}。")
        elif event.kind == "wait":
            self.write(f"System：{name} 本 Tick 选择等待。")
        # reveal 仍在裁判事件与赛后公开数据中；终端和 chat.log 由 result 统一展示。
        elif event.kind in ("retry", "error", "chat_error"):
            self.write(f"System：{name}，{display_text(event.text)}")
        elif event.kind in ("reminder", "settlement_pending", "handoff"):
            self.write(f"System：{display_text(event.text)}")

    def result(self, state: GameState) -> str:
        result = state.result
        if result is None:
            raise ValueError("对局尚未结束。")
        if result.reason == EndReason.TECHNICAL_ERROR:
            text = "技术异常中止；本局不计胜负。"
        elif result.reason == EndReason.NO_MOVES:
            text = "双方到期未出拳，平局。"
        elif result.reason == EndReason.FORFEIT:
            loser = "B" if result.winner == "A" else "A"
            text = f"{self.names[loser]} 到期未出拳，视为弃权；{self.names[result.winner]} 获胜。"
        elif result.winner is None:
            text = "双方拳型相同，平局。"
        else:
            text = f"{self.names[result.winner]} 获胜。"
        if result.reason != EndReason.TECHNICAL_ERROR:
            moves = '；'.join(f"{self.names[player]} → {GESTURE_NAMES[move] if move is not None else '未出拳'}"
                             for player, move in zip(('A', 'B'), state.moves))
            text = f'{moves}；{text}'
        line = f"System：{text}"
        self.write(line)
        return line
