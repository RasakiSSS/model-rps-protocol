"""模型对话历史与已提供的公开事件；增量追加，保留整场原始记忆。"""

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryCheckpoint:
    message_count: int
    cursors: dict[int, int]
    results: frozenset[int]


class ConversationHistory:
    def __init__(self, system_prompt):
        self.messages = [{"role": "system", "content": system_prompt}]
        self.cursors = {}
        self.results = set()
        self._game_snapshot = None
        self._postmatch_snapshot = None

    def checkpoint(self):
        return HistoryCheckpoint(len(self.messages), dict(self.cursors), frozenset(self.results))

    def restore(self, checkpoint):
        del self.messages[checkpoint.message_count:]
        self.cursors = dict(checkpoint.cursors)
        self.results = set(checkpoint.results)
        self._game_snapshot = self._postmatch_snapshot = None

    def _game_record(self, record):
        """按原始事件顺序切片；结果与首次公开的分析不因事件去重丢失。"""
        record = deepcopy(record)
        number = record["game"]
        events = record.get("events", [])
        cursor = self.cursors.get(number, 0)
        record["history_after"] = cursor
        record["event_cursor"] = len(events)
        record["events"] = [{**event, "sequence": index} for index, event in enumerate(events, 1)
                            if index > cursor]
        return record

    def game_context(self, view, match, feedback):
        # 同一交互重试保持原快照，即使上次无效响应已进入自己的历史。
        if feedback is None or self._game_snapshot is None:
            state = view.to_dict()
            number = match["game"] if match is not None else 1
            cursor = self.cursors.get(number, 0)
            state["history_after"] = cursor
            state["history"] = [event for event in state["history"] if event["sequence"] > cursor]
            context = {"phase": "game" if view.can_act else "chat", "state": state}
            if match is not None:
                context["match"] = deepcopy(match)
                previous = match.get("last_game")
                if previous is not None:
                    if previous["game"] in self.results:
                        context["match"].pop("last_game")
                    else:
                        context["match"]["last_game"] = self._game_record(previous)
            self._game_snapshot = context
        context = deepcopy(self._game_snapshot)
        context["correction"] = feedback
        return context

    def postmatch_context(self, context, feedback):
        if feedback is None or self._postmatch_snapshot is None:
            context = deepcopy(context)
            if "games" in context.get("match", {}):
                context["match"]["games"] = [self._game_record(game) for game in context["match"]["games"]]
            self._postmatch_snapshot = context
        context = deepcopy(self._postmatch_snapshot)
        context["correction"] = feedback
        return context

    def remember(self, context, user_message, content):
        """完整 HTTP 响应后记录；若整次交互未应用，调用方连同检查点一起回退。"""
        self.messages.extend([user_message, {"role": "assistant", "content": content}])
        if "state" in context:
            number = context.get("match", {}).get("game", 1)
            self.cursors[number] = context["state"]["event_cursor"]
            previous = context.get("match", {}).get("last_game")
            if previous is not None:
                self.cursors[previous["game"]] = previous["event_cursor"]
                self.results.add(previous["game"])
        elif "match" in context:
            for game in context["match"].get("games", []):
                self.cursors[game["game"]] = game["event_cursor"]
                self.results.add(game["game"])
