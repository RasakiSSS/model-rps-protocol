"""公开聊天与游戏行动分别推进；每席位一个请求，行动优先于可选聊天。"""

import asyncio
from dataclasses import dataclass, replace

from ..core.commands import Interaction
from ..core.game import ActionKind, Event, GameState, Initiative, InvalidResponse, PlayerView, abort, advance, advance_turn, ignored_move
from .requests import RequestPolicy, TurnFailed, request_interaction


@dataclass
class PendingRequest:
    view: PlayerView
    task: asyncio.Task
    checkpoint: object = None
    actor: str | None = None


@dataclass
class HandoffRequest:
    identity: str
    human: object
    epoch: int
    replacement: object
    result: asyncio.Future


class GameSession:
    def __init__(self, a, b, *, state: GameState, policy: RequestPolicy, on_event=None, on_request=None):
        self.players = {"A": a, "B": b}
        self.state = state
        self.policy = policy
        self.emit = on_event or (lambda event: None)
        self.on_request = on_request
        self.pending: dict[str, PendingRequest] = {}
        self.held = {}
        self.held_requests = {}
        self.inboxes = {"A": [], "B": []}
        # 状态通知与聊天消息分别排队；同类通知合并，只在派发时消费。
        self.wake_reasons = {identity: {"game_start"} for identity in self.players}
        self.serial = 0
        self.announced_tick = 0
        self.controls = asyncio.Queue()
        self.control_wait = None
        self.handoffs = []
        self.active = False
        for identity, seat in self.players.items():
            if getattr(seat, "interactive", False) and getattr(seat, "prepare_handoff", None):
                human = seat.player

                async def handoff(profile, epoch, identity=identity, human=human):
                    if not self.active or self.state.result is not None or epoch != human.epoch:
                        raise InvalidResponse("本局已结束或输入已过期，请在新的行动状态下重试。")
                    if any(not item.result.done() for item in self.handoffs):
                        raise InvalidResponse("已有交接正在处理。")
                    try:
                        replacement = self.players[identity].prepare_handoff(profile)
                    except ValueError as exc:
                        raise InvalidResponse(str(exc)) from None
                    request = HandoffRequest(identity, human, epoch, replacement,
                                             asyncio.get_running_loop().create_future())
                    self.handoffs.append(request)
                    self.controls.put_nowait(request)
                    await request.result

                human.on_handoff = handoff

    def _actor(self, identity):
        return getattr(self.players[identity], "name", None)

    def _view(self, identity):
        view = self.state.view_for(identity)
        own_move = view.own_move
        for action in self.held.get(identity, ()):
            if action.kind == ActionKind.MOVE and not ignored_move(action, own_move, view.move_policy):
                own_move = action.move
        return replace(view, can_act=self._needs_action(identity), own_move=own_move)

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
            interactive = getattr(player, "interactive", False)
            pending = self.pending.get(identity)
            if pending is not None and not interactive:
                continue
            can_act = self._needs_action(identity)
            reasons = sorted(self.wake_reasons[identity])
            if can_act:
                reasons.append("action_available")
            if self.inboxes[identity]:
                reasons.append("opponent_say")
            if not reasons and not interactive:
                continue
            view = self._view(identity)
            if pending is None:
                self.serial += 1
            view = replace(view, can_act=can_act,
                           request_id=pending.view.request_id if pending else self.serial,
                           notifications=tuple(self.inboxes[identity]), request_reasons=tuple(reasons))
            self.inboxes[identity].clear()
            self.wake_reasons[identity].clear()
            if interactive:
                player.update_view(view)
            if pending is not None:
                pending.view = view
                continue
            checkpoint = getattr(player, "checkpoint_history", lambda: None)()
            actor = self._actor(identity)

            def record(data, actor=actor):
                if self.on_request is not None:
                    self.on_request({**data, **({"actor": actor} if actor else {})})

            task = asyncio.create_task(request_interaction(player, view, self.policy, self.emit, record))
            self.pending[identity] = PendingRequest(view, task, checkpoint, actor)

    def _record(self, request: PendingRequest, kind: str, **data):
        if self.on_request is not None:
            view = request.view
            self.on_request({"kind": kind, "player": view.player, "tick": view.tick,
                             "request_id": view.request_id, "phase": "game" if view.can_act else "chat",
                             **({"actor": request.actor} if request.actor else {}), **data})

    def _accept(self, request: PendingRequest, interaction: Interaction):
        identity = request.view.player
        opponent = "B" if identity == "A" else "A"
        sequences = []
        tick = self.state.completed_ticks + 1
        alternating_action = interaction.decision is not None and self.state.initiative == Initiative.ALTERNATING
        for text in (() if alternating_action else interaction.messages):
            event = Event(self.state.completed_ticks + 1, "say", identity, text, request.actor)
            self.state = replace(self.state, events=(*self.state.events, event))
            sequence = len(self.state.events)
            sequences.append(sequence)
            self.inboxes[opponent].append(sequence)
            self.emit(event)
        if interaction.decision is not None:
            # SAY 已公开，不再在裁判推进时重放；提交内容在同步边界前仅本人可见。
            self.held[identity] = tuple(action for action in interaction.decision.actions
                                        if alternating_action or action.kind != ActionKind.SAY)
            self.held_requests[identity] = request
            remember = getattr(self.players[identity], "remember_decision", None)
            if remember is not None:
                remember(request.view, interaction.decision, request.actor)
        sequences.extend(self._finish_ready())
        if (interaction.decision is not None and self.state.result is None
                and self.state.initiative == Initiative.SIMULTANEOUS and identity in self.held):
            # 已结束自己的行动、仍在等对方时，主动获得一次聊天机会。
            # 只通知本人，不向对手提前公开提交状态。
            self.wake_reasons[identity].add("action_ended")
        self._record(request, "interaction_applied", applied_tick=tick,
                     event_sequences=sequences, ended_turn=interaction.decision is not None,
                     pending_action=identity in self.held, message_count=len(interaction.messages),
                     silent=not interaction.messages and interaction.decision is None)

    def _finish_ready(self):
        previous = self.state
        requests = {}
        if previous.initiative == Initiative.SIMULTANEOUS:
            if len(self.held) != 2:
                return []
            self.state = advance(previous, self.held.pop("A"), self.held.pop("B"))
            requests = self.held_requests.copy()
            self.held_requests.clear()
        else:
            identity = previous.current_player
            if identity not in self.held:
                return []
            self.state = advance_turn(previous, identity, self.held.pop(identity))
            requests[identity] = self.held_requests.pop(identity)
        actors = {identity: request.actor for identity, request in requests.items()}
        # 已结束的行动可以在换人后才进入同步边界；作者仍是提交它的人。
        events = tuple(replace(event, actor=actors.get(event.player, self._actor(event.player))
                               if event.player is not None else None)
                       for event in self.state.events[len(previous.events):])
        self.state = replace(self.state, events=(*previous.events, *events))
        sequences = []
        for sequence, event in enumerate(self.state.events[len(previous.events):], len(previous.events) + 1):
            if event.kind == "say":
                sequences.append(sequence)
                self.inboxes["B" if event.player == "A" else "A"].append(sequence)
            self.emit(event)
        for identity, request in requests.items():
            self._record(request, "action_resolved", applied_tick=self.state.completed_ticks,
                         outcomes=[event.kind for event in events if event.player == identity and
                                   event.kind in ("commit", "revise", "wait", "move_ignored")],
                         own_move=self.state.moves[0 if identity == "A" else 1])
        if self.state.result is None:
            reason = "turn_changed" if self.state.initiative == Initiative.ALTERNATING else "tick_started"
            for reasons in self.wake_reasons.values():
                reasons.add(reason)
        self._announce()
        return sequences

    async def _handoff(self, request):
        if request.result.done():
            return
        identity, human = request.identity, request.human
        seat = self.players[identity]
        try:
            if seat.player is not human or human.epoch != request.epoch or self.state.result is not None:
                raise InvalidResponse("本局已结束或输入已过期，请在新的行动状态下重试。")
            pending = self.pending.get(identity)
            # Enter 已确认的响应先应用，不能因控制命令更快被唤醒而丢失。
            if pending is not None and (pending.task.done() or human.has_confirmed_input):
                response = await pending.task
                del self.pending[identity]
                pending.view = response.view
                self._accept(pending, response.interaction)
                pending = None
            if self.state.result is not None:
                raise InvalidResponse("本局已结束，交接未生效；如还有下一局，请重新输入。")
            replacement = request.replacement
            context = seat.handoff_context(self._view(identity), replacement)
            replacement.player.start_match()
            replacement.player.start_game(seat.context)
            replacement.player.receive_handoff(context)
            if pending is not None:
                pending.task.cancel()
                await asyncio.gather(pending.task, return_exceptions=True)
                del self.pending[identity]
                self._record(pending, "request_discarded", reason="人类已交接；未提交输入作废。")
            # 控制输入自身被取消时保留原玩家；下次调度重新开启人类输入。
            if request.result.cancelled():
                return
            previous_name = seat.name
            discarded_drafts = {"move": human.move is not None, "analysis_count": len(human.analysis)}
            seat.replace_player(replacement)
            human.detach()
            event = Event(self.state.completed_ticks + 1, "handoff", identity,
                          f"{previous_name} 已交接给 {replacement.name}；席位与比赛进度保留。", replacement.name)
            self.state = replace(self.state, events=(*self.state.events, event))
            for reasons in self.wake_reasons.values():
                reasons.add("handoff")
            self.emit(event)
            if self.on_request is not None:
                self.on_request({"kind": "handoff", "player": identity, "tick": event.tick,
                                 "actor": replacement.name, "previous_player": previous_name,
                                 "profile": replacement.profile, "completed_games": len(context["games"]),
                                 "public_events": len(context["state"]["history"]) +
                                     sum(len(game["events"]) for game in context["games"]),
                                 "own_decisions": len(context["own_decisions"]),
                                 "own_move": context["state"]["own_move"],
                                 "own_move_locked": context["state"]["own_move_locked"],
                                 "can_act": context["state"]["can_act"], "discarded_drafts": discarded_drafts})
            request.result.set_result(None)
        except ValueError as exc:
            request.result.set_exception(InvalidResponse(str(exc)))

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
        for request in self.held_requests.values():
            self._record(request, "action_discarded", reason="本局结束或取消，尚未推进的行动作废。")

    async def run(self) -> GameState:
        self.active = True
        self.control_wait = asyncio.create_task(self.controls.get())
        try:
            while self.state.result is None:
                self._announce()
                self._dispatch()
                done, _ = await asyncio.wait([self.control_wait, *(r.task for r in self.pending.values())],
                                             return_when=asyncio.FIRST_COMPLETED)
                # 同次唤醒中优先完成游戏行动和结算，再处理可选聊天。
                ready = sorted((r for r in self.pending.values() if r.task in done),
                               key=lambda r: (not r.view.can_act, r.view.request_id))
                for request in ready:
                    identity = request.view.player
                    del self.pending[identity]
                    try:
                        response = request.task.result()
                    except TurnFailed as exc:
                        if request.view.can_act:
                            self.state = abort(self.state, identity, f"{exc} 本局中止。")
                            self.emit(self.state.events[-1])
                        else:
                            # 可选聊天失败不扣行动、不判输；等新消息或状态变化才再次请求。
                            self.emit(Event(self.state.completed_ticks + 1, "chat_error", identity,
                                            f"聊天请求未完成，本次跳过：{exc}"))
                    else:
                        request.view = response.view
                        self._accept(request, response.interaction)
                    if self.state.result is not None:
                        break
                if self.control_wait in done:
                    await self._handoff(self.control_wait.result())
                    self.control_wait = asyncio.create_task(self.controls.get())
            return self.state
        finally:
            self.active = False
            self.control_wait.cancel()
            await asyncio.gather(self.control_wait, return_exceptions=True)
            for request in self.handoffs:
                if not request.result.done():
                    request.result.set_exception(InvalidResponse("比赛已结束或中止，交接未生效。"))
            await self._close_requests()
