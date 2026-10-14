import asyncio
import unittest

from model_rps.game import EndReason, Gesture
from model_rps.players import PlayerError, ScriptedPlayer, WAIT_RESPONSE
from model_rps.runner import RequestPolicy, run_game

ROCK = '{"decision_summary":"测试决策说明","actions":[{"type":"MOVE","move":"Rock"}]}'
SCISSORS = '{"decision_summary":"测试决策说明","actions":[{"type":"MOVE","move":"Scissors"}]}'
FAST = RequestPolicy(timeout=1, retry_delay=0)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_requests_start_before_either_finishes(self):
        started = [asyncio.Event(), asyncio.Event()]

        class RendezvousPlayer:
            def __init__(self, index, response):
                self.index, self.response = index, response

            async def respond(self, view, feedback=None):
                started[self.index].set()
                await started[1 - self.index].wait()
                return self.response

        state = await run_game(RendezvousPlayer(0, ROCK), RendezvousPlayer(1, SCISSORS), policy=FAST)
        self.assertEqual(state.result.winner, "A")

    async def test_retry_uses_same_snapshot_and_does_not_repeat_successful_peer(self):
        a = ScriptedPlayer([ROCK])
        b = ScriptedPlayer(['{"decision_summary":"测试决策说明","actions":[{"type":"WAIT"},{"type":"MOVE","move":"Paper"}]}', SCISSORS])
        state = await run_game(a, b, policy=FAST)
        self.assertEqual(state.completed_ticks, 2)
        self.assertEqual(len(a.requests), 2)
        self.assertEqual(len(b.requests), 3)
        first, second = b.requests[:2]
        self.assertIs(first[0], second[0])
        self.assertFalse(second[0].opponent_committed)
        self.assertEqual(second[0].history, ())
        self.assertIn("WAIT", second[1])
        self.assertEqual(state.result.winner, "A")

    async def test_format_and_network_failures_share_three_attempts(self):
        a = ScriptedPlayer([TimeoutError(), "invalid", ROCK])
        b = ScriptedPlayer([SCISSORS])
        state = await run_game(a, b, policy=FAST)
        self.assertEqual(len(a.requests), 4)
        self.assertEqual(state.result.winner, "A")
        self.assertEqual(state.completed_ticks, 2)

    async def test_settlement_retry_keeps_same_snapshot_and_can_reverse_winner(self):
        paper = '{"decision_summary":"最后改拳","actions":[{"type":"MOVE","move":"Paper"}]}'
        a = ScriptedPlayer([ROCK, "invalid", paper])
        b = ScriptedPlayer([ROCK, SCISSORS])
        events = []
        state = await run_game(a, b, policy=FAST, on_event=events.append)
        self.assertEqual(state.completed_ticks, 2)
        self.assertEqual(state.result.winner, "B")
        self.assertIs(a.requests[1][0], a.requests[2][0])
        view = a.requests[2][0]
        self.assertTrue(view.is_settlement_tick)
        self.assertTrue(view.opponent_committed)
        self.assertNotIn("Scissors", str(view.to_dict()))
        self.assertEqual([v.tick for v, _ in b.requests], [1, 2])
        self.assertEqual([e.tick for e in events if e.kind == "reveal"], [2, 2])

    async def test_failed_settlement_discards_revisions_without_showdown(self):
        a = ScriptedPlayer([ROCK, "invalid", "invalid", "invalid"])
        b = ScriptedPlayer([SCISSORS, ROCK])
        state = await run_game(a, b, policy=FAST)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual(state.completed_ticks, 1)
        self.assertEqual(state.moves, (Gesture.ROCK, Gesture.SCISSORS))
        self.assertEqual(state.settlement_tick, 2)
        self.assertFalse(any(e.kind == "reveal" for e in state.events))

    async def test_exhausted_errors_discard_current_tick_and_keep_previous_state(self):
        a = ScriptedPlayer([ROCK, "invalid", TimeoutError(), PlayerError("暂时不可用")])
        b = ScriptedPlayer([WAIT_RESPONSE, SCISSORS])
        state = await run_game(a, b, policy=FAST)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertIsNone(state.result.winner)
        self.assertEqual(state.completed_ticks, 1)
        self.assertEqual(state.moves, (Gesture.ROCK, None))
        self.assertEqual(len(a.requests), 4)
        self.assertEqual(len(b.requests), 2)
        self.assertFalse(any(e.kind == "reveal" for e in state.events))

    async def test_timeout_cancels_each_attempt_and_is_bounded(self):
        class SlowPlayer:
            calls = 0
            cancellations = 0

            async def respond(self, view, feedback=None):
                self.calls += 1
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    self.cancellations += 1
                    raise

        a = SlowPlayer()
        b = ScriptedPlayer([SCISSORS])
        state = await run_game(a, b, policy=RequestPolicy(timeout=0.01, retry_delay=0))
        self.assertEqual(a.calls, 3)
        self.assertEqual(a.cancellations, 3)
        self.assertEqual(len(b.requests), 1)
        self.assertEqual(state.completed_ticks, 0)
        self.assertEqual(state.moves, (None, None))
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)

    async def test_permanent_error_cancels_waiting_peer(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class Failing:
            async def respond(self, view, feedback=None):
                await started.wait()
                raise PlayerError("模型服务返回 HTTP 401", retryable=False)

        class Pending:
            async def respond(self, view, feedback=None):
                started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()

        state = await run_game(Failing(), Pending(), policy=FAST)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)

    async def test_unexpected_exception_does_not_print_secret(self):
        events = []
        state = await run_game(ScriptedPlayer([RuntimeError("sensitive-key")]),
                               ScriptedPlayer([SCISSORS]), policy=FAST, on_event=events.append)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertNotIn("sensitive-key", repr(events))

    async def test_external_cancellation_cleans_up_both_requests(self):
        started = [asyncio.Event(), asyncio.Event()]
        cancelled = []

        class Pending:
            def __init__(self, index):
                self.index = index

            async def respond(self, view, feedback=None):
                started[self.index].set()
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.append(self.index)

        task = asyncio.create_task(run_game(Pending(0), Pending(1), policy=FAST))
        await asyncio.gather(*(event.wait() for event in started))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sorted(cancelled), [0, 1])

    async def test_next_tick_sees_public_history_and_commit_status(self):
        a = ScriptedPlayer([ROCK, WAIT_RESPONSE])
        b = ScriptedPlayer([WAIT_RESPONSE, SCISSORS])
        state = await run_game(a, b, policy=FAST)
        self.assertTrue(b.requests[1][0].opponent_committed)
        self.assertEqual(b.requests[1][0].tick, 2)
        self.assertEqual([e.kind for e in b.requests[1][0].history], ["commit", "wait"])
        self.assertEqual(state.result.winner, "A")

    def test_invalid_policy_is_rejected(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                RequestPolicy(timeout=timeout)
        with self.assertRaises(ValueError):
            RequestPolicy(retry_delay=-1)


if __name__ == "__main__":
    unittest.main()
