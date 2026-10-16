"""公开聊天与游戏行动分别推进；每席位一个请求，行动优先于可选聊天。"""

import asyncio
from dataclasses import dataclass, replace

from .commands import Interaction
from .game import ActionKind, Event, GameState, Initiative, PlayerView, abort, advance, advance_turn, ignored_move
from .requests import RequestPolicy, TurnFailed, request_interaction


@dataclass
class PendingRequest:
    view: PlayerView
    task: asyncio.Task
    checkpoint: object = None


class GameSession:
    def __init__(self, a, b, *, state: GameState, policy: RequestPolicy, on_event=None, on_request=None):
        self.players = {"A": a, "B": b}
        self.state = state
        self.policy = policy
        self.emit = on_event or (lambda event: None)
        self.on_request = on_request
        self.pending: dict[str, PendingRequest] = {}
        self.held = {}
        self.inboxes = {"A": [], "B": []}
        # 状态通知与聊天消息分别排队；同类通知合并，只在派发时消费。
        self.wake_reasons = {identity: {"game_start"} for identity in self.players}
        self.serial = 0
        self.announced_tick = 0

    def _needs_action(self, player: str) -> bool:
        return player not in self.held and (self.state.initiative == Initiative.SIMULTANEOUS
                                            or self.state.current_player == player)

    def _announce(self):
        tick = self.state.completed_ticks + 1
        if self.announced_tick == tick or self.state.result is not None:
            return
        self.announced_tick = tick
        if self.state.initiative == Initiative.ALTERNATING:
            view = self.state.view_for(self.state.current_player)
            self.emit(Event(tick, "turn", view.player, str(view.own_turn)))
        else:
            self.emit(Event(tick, "tick", text="settlement" if tick == self.state.settlement_tick else ""))

    def _dispatch(self):
        # 行动机会同时消化所有未读消息，不为同一批消息额外安排聊天请求。
        for identity, player in self.players.items():
            if identity in self.pending:
                continue
            can_act = self._needs_action(identity)
            reasons = sorted(self.wake_reasons[identity])
            if can_act:
                reasons.append("action_available")
            if self.inboxes[identity]:
                reasons.append("opponent_say")
            if not reasons:
                continue
            view = self.state.view_for(identity)
            own_move = view.own_move
            for action in self.held.get(identity, ()):
                if action.kind == ActionKind.MOVE and not ignored_move(action, own_move, view.move_policy):
                    own_move = action.move
            self.serial += 1
            view = replace(view, can_act=can_act, own_move=own_move, request_id=self.serial,
                           notifications=tuple(self.inboxes[identity]), request_reasons=tuple(reasons))
            self.inboxes[identity].clear()
            self.wake_reasons[identity].clear()
            checkpoint = getattr(player, "checkpoint_history", lambda: None)()
            task = asyncio.create_task(request_interaction(player, view, self.policy, self.emit, self.on_request))
            self.pending[identity] = PendingRequest(view, task, checkpoint)

    def _record(self, request: PendingRequest, kind: str, **data):
        if self.on_request is not None:
            view = request.view
            self.on_request({"kind": kind, "player": view.player, "tick": view.tick,
                             "request_id": view.request_id, "phase": "game" if view.can_act else "chat", **data})

    def _accept(self, request: PendingRequest, interaction: Interaction):
        identity = request.view.player
        opponent = "B" if identity == "A" else "A"
        sequences = []
        tick = self.state.completed_ticks + 1
        alternating_action = interaction.decision is not None and self.state.initiative == Initiative.ALTERNATING
        for text in (() if alternating_action else interaction.messages):
            event = Event(self.state.completed_ticks + 1, "say", identity, text)
            self.state = replace(self.state, events=(*self.state.events, event))
            sequence = len(self.state.events)
            sequences.append(sequence)
            self.inboxes[opponent].append(sequence)
            self.emit(event)
        if interaction.decision is not None:
            # SAY 已公开，不再在裁判推进时重放；提交内容在同步边界前仅本人可见。
            self.held[identity] = tuple(action for action in interaction.decision.actions
                                        if alternating_action or action.kind != ActionKind.SAY)
        sequences.extend(self._finish_ready())
        if (interaction.decision is not None and self.state.result is None
                and self.state.initiative == Initiative.SIMULTANEOUS and identity in self.held):
            # 已结束自己的行动、仍在等对方时，主动获得一次聊天机会。
            # 只通知本人，不向对手提前公开提交状态。
            self.wake_reasons[identity].add("action_ended")
        self._record(request, "interaction_applied", applied_tick=tick,
                     event_sequences=sequences, ended_turn=interaction.decision is not None,
                     silent=not interaction.messages and interaction.decision is None)

    def _finish_ready(self):
        previous = self.state
        if previous.initiative == Initiative.SIMULTANEOUS:
            if len(self.held) != 2:
                return []
            self.state = advance(previous, self.held.pop("A"), self.held.pop("B"))
        else:
            identity = previous.current_player
            if identity not in self.held:
                return []
            self.state = advance_turn(previous, identity, self.held.pop(identity))
        sequences = []
        for sequence, event in enumerate(self.state.events[len(previous.events):], len(previous.events) + 1):
            if event.kind == "say":
                sequences.append(sequence)
                self.inboxes["B" if event.player == "A" else "A"].append(sequence)
            self.emit(event)
        if self.state.result is None:
            reason = "turn_changed" if self.state.initiative == Initiative.ALTERNATING else "tick_started"
            for reasons in self.wake_reasons.values():
                reasons.add(reason)
        self._announce()
        return sequences

    async def _close_requests(self):
        # 先等待取消生效，再回退未应用响应的历史；下一局不能与旧请求重叠。
        requests = tuple(self.pending.values())
        for request in requests:
            request.task.cancel()
        await asyncio.gather(*(request.task for request in requests), return_exceptions=True)
        self.pending.clear()
        for request in requests:
            if request.checkpoint is not None:
                self.players[request.view.player].restore_history(request.checkpoint)
        for request in requests:
            self._record(request, "request_discarded", reason="对局结束或取消；响应未应用，历史已回退。")

    async def run(self) -> GameState:
        try:
            while self.state.result is None:
                self._announce()
                self._dispatch()
                done, _ = await asyncio.wait([r.task for r in self.pending.values()],
                                             return_when=asyncio.FIRST_COMPLETED)
                # 同次唤醒中优先完成游戏行动和结算，再处理可选聊天。
                ready = sorted((r for r in self.pending.values() if r.task in done),
                               key=lambda r: (not r.view.can_act, r.view.request_id))
                for request in ready:
                    identity = request.view.player
                    del self.pending[identity]
                    try:
                        interaction = request.task.result()
                    except TurnFailed as exc:
                        if request.view.can_act:
                            self.state = abort(self.state, identity, f"{exc} 本局中止。")
                            self.emit(self.state.events[-1])
                        else:
                            # 可选聊天失败不扣行动、不判输；等新消息或状态变化才再次请求。
                            self.emit(Event(self.state.completed_ticks + 1, "chat_error", identity,
                                            f"聊天请求未完成，本次跳过：{exc}"))
                    else:
                        self._accept(request, interaction)
                    if self.state.result is not None:
                        break
            return self.state
        finally:
            await self._close_requests()
