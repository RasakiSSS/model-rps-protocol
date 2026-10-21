"""外部玩家：复用外部输入等待机制，完整指令仍经共同裁判校验。"""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

from ..core.commands import parse_interaction, parse_postmatch
from ..core.game import InvalidResponse, validate_turn
from .human import HumanPlayer
from .base import PlayerInput


class ExternalPlayer(HumanPlayer):
    input_source = "mcp"

    def __init__(self, **kwargs):
        self.token_namespace = uuid4().hex
        super().__init__(**kwargs)

    @property
    def can_submit(self):
        return self._available.is_set() and self._future is not None and not self._future.done() and not self.exiting

    @property
    def turn_token(self):
        if not self.can_submit:
            return None
        request = self.view.request_id if self.phase == "game" and self.view else self.postmatch_context.get("request_id", 0)
        tick = self.view.tick if self.phase == "game" and self.view else 0
        return f"{self.token_namespace}:{self.epoch}:{self.phase}:{request}:{tick}:{int(self.can_act)}"

    async def _receive(self):
        if self._future is not None:
            raise RuntimeError("外部玩家不能同时处理多个输入请求。")
        future = self._future = asyncio.get_running_loop().create_future()
        self._available.set()
        self.on_update()
        try:
            return await future
        finally:
            self._available.clear()
            self._future = None
            self.on_update()

    def update_postmatch(self, context):
        # 持续更新的退出状态不能覆盖首次收到的公开比赛报告。
        self.postmatch_context.update(context)
        self.on_update()

    def submit_actions(self, actions, turn_token):
        if not self.can_submit or turn_token != self.turn_token:
            raise InvalidResponse("行动机会已变化或输入仍在处理中；请读取最新状态后重试。")
        raw = json.dumps({"actions": actions}, ensure_ascii=False, allow_nan=False)
        if self.phase == "postmatch":
            response = parse_postmatch(raw)
            self.exiting = response.exit
            submission = raw
        elif self.phase == "game" and self.view is not None:
            view = replace(self.view, can_act=self.can_act)
            interaction = parse_interaction(raw, can_act=view.can_act, analysis=self.analysis_policy)
            if interaction.decision is not None:
                validate_turn(interaction.decision.actions, view.own_move, view.move_policy)
                self._submitted_action = (self.epoch, view.tick)
            submission = PlayerInput(raw, view)
        else:
            raise InvalidResponse("当前阶段不能提交指令。")
        self._available.clear()
        self._future.set_result(submission)
        self.on_update()
