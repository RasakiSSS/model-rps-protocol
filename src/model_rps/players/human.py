"""人类玩家适配器：私有行动草稿与统一指令，不读取裁判隐藏状态。"""

import asyncio
from copy import deepcopy
import json

from ..core.commands import AnalysisPolicy, CommandKind, parse_command_line, parse_interaction, parse_postmatch
from ..core.game import InvalidResponse, validate_turn
from .base import PlayerInput


class HumanPlayer:
    interactive = True

    def __init__(self, *, analysis=AnalysisPolicy.OPTIONAL):
        self.analysis_policy = AnalysisPolicy(analysis)
        self.on_update = lambda: None
        self.on_boundary = lambda: None
        self.on_handoff = None
        self._available = asyncio.Event()
        self._future = None
        self.epoch = 0
        self.start_match()

    def start_match(self):
        self.epoch += 1
        self.phase = "waiting"
        self.view = None
        self.match = {}
        self.postmatch_context = {}
        self.move = None
        self.analysis = []
        self.exiting = False
        self._submitted_action = None

    def start_game(self, context):
        self.epoch += 1
        self.phase = "game"
        self.match = deepcopy(context)
        self.view = None
        self.move, self.analysis = None, []
        self._submitted_action = None
        self.on_boundary()

    def update_view(self, view):
        self.view = view
        self.on_update()

    def update_postmatch(self, context):
        self.postmatch_context = deepcopy(context)
        self.on_update()

    @property
    def can_act(self):
        return (self.phase == "game" and self.view is not None and self.view.can_act
                and self._submitted_action != (self.epoch, self.view.tick))

    @property
    def has_confirmed_input(self):
        return self._future is not None and self._future.done() and not self._future.cancelled()

    async def _receive(self):
        if self._future is not None:
            raise RuntimeError("人类玩家不能同时处理多个输入请求。")
        future = self._future = asyncio.get_running_loop().create_future()
        self._available.set()
        try:
            return await future
        finally:
            self._available.clear()
            self._future = None

    async def respond(self, view, feedback=None):
        return await self._receive()

    async def postmatch(self, context, feedback=None):
        if self.phase != "postmatch":
            self.epoch += 1
            self.phase = "postmatch"
            self.view = None
            self.move, self.analysis = None, []
            self.on_boundary()
        self.update_postmatch(context)
        return await self._receive()

    @property
    def status(self):
        if self.phase == "handed_off":
            return "模型已接管 · 观看比赛"
        if self.phase == "postmatch":
            return "赛后聊天 · /say 发言，/exit 退出"
        if self.view is None:
            return "等待比赛状态"
        permission = "可行动" if self.can_act else "仅聊天"
        return f"第 {self.match.get('game', 1)} 局 · Tick {self.view.tick} · {permission}"

    def describe(self):
        if self.view is None:
            return self.status
        view = self.view
        own = view.own_move.value if view.own_move else "未出拳"
        draft = self.move.move.value if self.move else "无"
        return (f"{self.status}；自己的拳型：{own}；对手{'已' if view.opponent_committed else '未'}公开提交；"
                f"本方剩余机会：{view.to_dict()['remaining_turns_including_current']}；"
                f"暂存出拳：{draft}；暂存分析 {len(self.analysis)} 段；分析要求：{self.analysis_policy}。")

    def detach(self):
        self.epoch += 1
        self.phase = "handed_off"
        self.exiting = True
        self.view = None
        self.move, self.analysis = None, []
        self.on_handoff = None
        self.on_boundary()

    async def submit(self, line, *, epoch=None):
        """单条本地输入；错误只反馈本人，已提交响应才进入公共调度。"""
        epoch = self.epoch if epoch is None else epoch
        if epoch != self.epoch:
            raise InvalidResponse("局面已切换，旧输入未提交，请重新输入。")
        if self.phase == "handed_off":
            raise InvalidResponse("本席位已交给模型，你现在是观众。")
        if line.strip().lower() == "/help":
            return ("/say 文本：立即公开；/move Rock|Paper|Scissors：暂存出拳；/analyze 文本：暂存私有分析；"
                    "/endturn：提交草稿并结束行动，无出拳则等待；/clear：清空草稿；/status：自己的状态；"
                    "/handoff profile：交给已配置模型，丢弃未提交草稿；赛后 /exit：退出聊天。Ctrl+C：中止整场运行。")
        if line.strip().lower() == "/status":
            return self.describe()
        if line.strip().lower() == "/clear":
            self.move, self.analysis = None, []
            return "已清空未提交的出拳与分析。"
        parts = line.strip().split()
        if parts and parts[0].lower() == "/handoff":
            if len(parts) != 2:
                raise InvalidResponse("用法：/handoff profile名称，例如 /handoff deepseek。")
            if self.phase != "game" or self.on_handoff is None:
                raise InvalidResponse("/handoff 仅能在比赛进行中交给已配置的模型。")
            await self.on_handoff(parts[1], epoch)
            return "已交接；未提交草稿已清空，你可继续观看比赛。"
        command = parse_command_line(line)
        if self.exiting:
            raise InvalidResponse("你已退出赛后聊天。")
        if command.kind in (CommandKind.MOVE, CommandKind.ANALYZE):
            if not self.can_act:
                raise InvalidResponse("当前仅能聊天，不能暂存出拳或分析。")
            if command.kind == CommandKind.MOVE:
                self.move = command
                if self.view.to_dict()["own_move_locked"]:
                    return "已有出拳已锁定，此 MOVE 提交后会被忽略；/endturn 仍可结束行动。"
                return f"已暂存 {command.move.value}，/endturn 时提交；再次 /move 可修改草稿。"
            if self.analysis_policy == AnalysisPolicy.OFF:
                raise InvalidResponse("本席位已关闭分析，不接受 /analyze。")
            self.analysis.append(command)
            return f"已暂存第 {len(self.analysis)} 段分析，/endturn 时提交：{command.text}"
        if command.kind == CommandKind.EXIT and self.phase != "postmatch":
            raise InvalidResponse("/exit 仅用于赛后聊天；中止整场运行请按 Ctrl+C。")
        if command.kind == CommandKind.ENDTURN and not self.can_act:
            raise InvalidResponse("当前没有行动权限，可以使用 /say 聊天。")
        tick = self.view.tick if self.view else None
        await self._available.wait()
        if epoch != self.epoch:
            raise InvalidResponse("局面已切换，旧输入未提交，请重新输入。")
        commands = [command]
        if command.kind == CommandKind.ENDTURN:
            if not self.can_act or self.view.tick != tick:
                raise InvalidResponse("行动机会已变化，请查看当前状态后重新提交。")
            commands = [*self.analysis, *([self.move] if self.move else []), command]
        raw = json.dumps({"actions": [c.to_dict() for c in commands]}, ensure_ascii=False)
        if self.phase == "postmatch":
            result = parse_postmatch(raw)
            self.exiting = result.exit
            submission = raw
        else:
            if self.view is None:
                raise InvalidResponse("比赛正在切换，请稍后输入。")
            interaction = parse_interaction(raw, can_act=self.can_act, analysis=self.analysis_policy)
            if interaction.decision is not None:
                validate_turn(interaction.decision.actions, self.view.own_move, self.view.move_policy)
                self._submitted_action = (self.epoch, self.view.tick)
                self.move, self.analysis = None, []
            submission = PlayerInput(raw, self.view)
        self._available.clear()
        self._future.set_result(submission)
        self.on_update()
        return None
