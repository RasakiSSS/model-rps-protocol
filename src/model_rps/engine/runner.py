"""单局入口；请求策略与事件调度分别由 requests、scheduler 负责。"""

from ..core.game import GameState, Initiative, MovePolicy
from .requests import MAX_ATTEMPTS, RequestPolicy
from .scheduler import GameSession


async def run_game(a, b, *, policy: RequestPolicy | None = None,
                   move_policy: MovePolicy = MovePolicy.LOCKED,
                   initiative: Initiative = Initiative.SIMULTANEOUS, first_player: str = "A",
                   on_event=None, on_request=None) -> GameState:
    session = GameSession(a, b, state=GameState(move_policy=move_policy, initiative=initiative,
                                               first_player=first_player),
                          policy=policy or RequestPolicy(), on_event=on_event, on_request=on_request)
    return await session.run()
