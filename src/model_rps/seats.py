"""固定席位、当前参与者，以及仅属于本场 BO 的交接资料。"""

from copy import deepcopy
from dataclasses import dataclass

from .game import ignored_move
from .prompts import HANDOFF_PROMPT


@dataclass(frozen=True)
class Replacement:
    player: object
    name: str
    profile: str


class SeatPlayer:
    """比赛始终引用同一席位；换人只替换输入适配器，不替换裁判状态。"""

    def __init__(self, player, name, *, prepare_handoff=None, on_handoff=None):
        self.player = player
        self.name = name
        self.prepare_handoff = prepare_handoff
        self.on_handoff = on_handoff or (lambda replacement: None)
        self.context = {}
        self.games = []
        self.decisions = []

    def __getattr__(self, name):
        return getattr(self.player, name)

    def start_match(self):
        self.context, self.games, self.decisions = {}, [], []
        self.player.start_match()

    def start_game(self, context):
        self.context = deepcopy(context)
        self.player.start_game(context)

    def finish_game(self, number, state):
        self.games.append({
            "game": number, "events": [event.to_dict() for event in state.events],
            "result": {"reason": state.result.reason.value, "winner": state.result.winner},
        })

    def remember_decision(self, view, decision, actor):
        self.decisions.append({
            "game": self.context.get("game", 1), "tick": view.tick, "player": view.player,
            "actor": actor, "summary": decision.summary,
            "actions": [action.to_dict() for action in decision.actions
                        if not ignored_move(action, view.own_move, view.move_policy)],
            "ignored_actions": [action.to_dict() for action in decision.actions
                                if ignored_move(action, view.own_move, view.move_policy)],
        })

    def handoff_context(self, view, replacement):
        match = {key: value for key, value in self.context.items() if key != "last_game"}
        return deepcopy({
            "phase": "handoff", "previous_player": self.name, "player": replacement.name,
            "request": HANDOFF_PROMPT, "match": match,
            "games": [{**game, "event_cursor": len(game["events"]),
                       "events": [{**event, "sequence": index} for index, event in enumerate(game["events"], 1)]}
                      for game in self.games], "state": view.to_dict(),
            "own_decisions": self.decisions,
        })

    def replace_player(self, replacement):
        self.player = replacement.player
        self.name = replacement.name
        self.on_handoff(replacement)
