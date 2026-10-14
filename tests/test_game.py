import json
import unittest

from model_rps.game import (
    Action, ActionKind, EndReason, GameState, Gesture, InvalidResponse,
    advance, judge, parse_response,
)

WAIT = (Action(ActionKind.WAIT),)


def move(value):
    return (Action(ActionKind.MOVE, move=Gesture(value)),)


class GameTests(unittest.TestCase):
    def test_all_nine_gesture_combinations(self):
        cases = [
            ("Rock", "Rock", None), ("Rock", "Paper", "B"), ("Rock", "Scissors", "A"),
            ("Paper", "Rock", "A"), ("Paper", "Paper", None), ("Paper", "Scissors", "B"),
            ("Scissors", "Rock", "B"), ("Scissors", "Paper", "A"), ("Scissors", "Scissors", None),
        ]
        for a, b, winner in cases:
            with self.subTest(a=a, b=b):
                state = advance(GameState(), move(a), move(b))
                self.assertEqual(state.result.reason, EndReason.SHOWDOWN)
                self.assertEqual(state.result.winner, winner)
                self.assertEqual(judge(Gesture(a), Gesture(b)), winner)
                self.assertEqual(len([e for e in state.events if e.kind == "reveal"]), 2)

    def test_change_move_before_other_player_submits(self):
        state = advance(GameState(), move("Rock"), WAIT)
        state = advance(state, move("Paper"), move("Scissors"))
        self.assertEqual(state.moves, (Gesture.PAPER, Gesture.SCISSORS))
        self.assertEqual(state.result.winner, "B")
        self.assertEqual(state.completed_ticks, 2)

    def test_all_actions_processed_before_judging(self):
        actions = parse_response(json.dumps({"decision_summary":"测试决策说明","actions": [
            {"type": "MOVE", "move": "Rock"}, {"type": "MOVE", "move": "Paper"},
            {"type": "SAY", "text": "出拳之后仍可说话"},
        ]}))
        state = advance(GameState(), actions, move("Scissors"))
        self.assertEqual(state.result.winner, "B")
        self.assertIn("出拳之后仍可说话", [e.text for e in state.events])
        reveals = [e for e in state.events if e.kind == "reveal"]
        self.assertEqual([e.text for e in reveals], ["Paper", "Scissors"])

    def test_wait_and_say_preserve_previous_move(self):
        state = advance(GameState(), move("Rock"), WAIT)
        state = advance(state, WAIT, WAIT)
        state = advance(state, (Action(ActionKind.SAY, text="继续等待"),), WAIT)
        self.assertEqual(state.moves, (Gesture.ROCK, None))
        self.assertIsNone(state.result)

    def test_opponent_snapshot_contains_commit_status_but_not_move(self):
        state = advance(GameState(), move("Rock"), WAIT)
        a = state.view_for("A").to_dict()
        b = state.view_for("B").to_dict()
        self.assertEqual(a["own_move"], "Rock")
        self.assertTrue(b["opponent_committed"])
        self.assertIsNone(b["own_move"])
        self.assertNotIn("Rock", json.dumps(b))
        self.assertNotIn("Rock", json.dumps([e.to_dict() for e in state.events]))
        self.assertEqual(b["tick"], 2)
        self.assertEqual(b["remaining_ticks_including_current"], 19)

    def test_snapshot_serialization_cannot_modify_original_history(self):
        state = advance(GameState(), (Action(ActionKind.SAY, text="hello"),), WAIT)
        view = state.view_for("A")
        payload = view.to_dict()
        payload["history"][0]["text"] = "changed"
        self.assertEqual(view.history[0].text, "hello")
        self.assertEqual(state.events[0].text, "hello")

    def test_reminders_and_double_no_move_draw(self):
        state = GameState()
        for _ in range(20):
            state = advance(state, WAIT, WAIT)
        reminders = [e for e in state.events if e.kind == "reminder"]
        self.assertEqual([e.tick for e in reminders], [10, 18, 19])
        self.assertEqual([e.text for e in reminders], [
            "距离本局强制结束还剩 10 Tick。", "距离本局强制结束还剩 2 Tick。",
            "距离本局强制结束还剩 1 Tick。",
        ])
        self.assertEqual(state.result.reason, EndReason.NO_MOVES)
        self.assertIsNone(state.result.winner)

    def test_forfeit_in_both_directions(self):
        for a, b, winner in ((move("Rock"), WAIT, "A"), (WAIT, move("Paper"), "B")):
            with self.subTest(winner=winner):
                state = advance(GameState(), a, b)
                for _ in range(19):
                    state = advance(state, WAIT, WAIT)
                self.assertEqual(state.result.reason, EndReason.FORFEIT)
                self.assertEqual(state.result.winner, winner)

    def test_last_tick_changes_and_first_submission_precede_deadline(self):
        state = advance(GameState(), move("Rock"), WAIT)
        for _ in range(18):
            state = advance(state, WAIT, WAIT)
        state = advance(state, move("Paper"), move("Scissors"))
        self.assertEqual(state.completed_ticks, 20)
        self.assertEqual(state.result.reason, EndReason.SHOWDOWN)
        self.assertEqual(state.result.winner, "B")

    def test_no_reminder_after_early_settlement(self):
        state = GameState()
        for _ in range(9):
            state = advance(state, WAIT, WAIT)
        state = advance(state, move("Rock"), move("Rock"))
        self.assertFalse(any(e.kind == "reminder" for e in state.events))
        with self.assertRaises(ValueError):
            state.view_for("A")
        with self.assertRaises(ValueError):
            advance(state, WAIT, WAIT)

    def test_invalid_batch_does_not_execute_other_players_actions(self):
        state = GameState()
        invalid = WAIT + move("Rock")
        with self.assertRaises(InvalidResponse):
            advance(state, move("Paper"), invalid)
        self.assertEqual(state, GameState())


class ResponseTests(unittest.TestCase):
    def test_decision_summary_is_required_and_private(self):
        from model_rps.game import parse_decision

        for summary in (None, "", "  ", 1, []):
            with self.subTest(summary=summary), self.assertRaises(InvalidResponse):
                parse_response(json.dumps({"decision_summary": summary, "actions": [{"type": "WAIT"}]}))
        with self.assertRaises(InvalidResponse):
            parse_response('{"actions":[{"type":"WAIT"}]}')
        raw = json.dumps({"decision_summary": "private-strategy", "actions": [{"type": "WAIT"}]})
        decision = parse_decision(raw)
        self.assertEqual(decision.summary, "private-strategy")
        state = advance(GameState(), decision.actions, WAIT)
        self.assertNotIn("private-strategy", repr(state))
        self.assertNotIn("private-strategy", json.dumps(state.view_for("B").to_dict()))

    def test_valid_multiple_actions_and_wait(self):
        result = parse_response('{"decision_summary":"测试决策说明","actions":[{"type":"SAY","text":"你好"},'
                                '{"type":"MOVE","move":"Rock"}]}')
        self.assertEqual(result, (Action(ActionKind.SAY, text="你好"), *move("Rock")))
        self.assertEqual(parse_response('{"decision_summary":"测试决策说明","actions":[{"type":"WAIT"}]}'), WAIT)

    def test_invalid_payloads_are_rejected_as_a_whole(self):
        cases = [
            "", " ", "not JSON", "```json\n{}\n```", "[]", "null", "{}",
            '{"decision_summary":"测试决策说明","actions":[]}', '{"decision_summary":"测试决策说明","actions":{}}', '{"decision_summary":"测试决策说明","actions":[null]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"WAIT"}],"extra":1}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"WAIT","text":"wrong"}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"WAIT"},{"type":"SAY","text":"hello"}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"WAIT"},{"type":"WAIT"}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"MOVE","move":"Lizard"}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"MOVE","move":null}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"SAY","text":" "}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"SAY","text":1}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"SAY","text":"hello"},{"type":"BAD"}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"WAIT"}],"actions":[{"type":"WAIT"}]}',
            '{"decision_summary":"测试决策说明","actions":[{"type":"SAY","text":NaN}]}',
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(InvalidResponse):
                parse_response(raw)


if __name__ == "__main__":
    unittest.main()
