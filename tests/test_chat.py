import asyncio
import json
import unittest

import httpx

from fixtures import turn_payload, postmatch_payload
from model_rps.commands import AnalysisPolicy, parse_interaction
from model_rps.diagnostics import LogError
from model_rps.game import EndReason, Gesture, InvalidResponse
from model_rps.match import MatchConfig, run_match
from model_rps.players import ChatCompletionPlayer, OPENAI, PlayerError, ScriptedPlayer, WAIT_RESPONSE
from model_rps.runner import RequestPolicy, run_game


FAST = RequestPolicy(2, 0)


def turn(move, say=None):
    actions = [{'type': 'MOVE', 'move': move}]
    if say:
        actions.append({'type': 'SAY', 'text': say})
    return json.dumps(turn_payload(actions, 'PRIVATE-ANALYSIS'))


def chat(*messages):
    return json.dumps({'actions': [{'type': 'SAY', 'text': text} for text in messages]})


class Actor:
    """在每个并发场景中检查同一席位始终只有一个请求。"""
    def __init__(self, handler):
        self.handler = handler
        self.views = []
        self.busy = False

    async def respond(self, view, feedback=None):
        if self.busy:
            raise AssertionError('overlapping requests')
        self.busy = True
        self.views.append((view, feedback))
        try:
            return await self.handler(view, feedback)
        finally:
            self.busy = False


class ChatProtocolTests(unittest.TestCase):
    def test_chat_and_silence_do_not_end_action_or_require_analysis(self):
        for policy in AnalysisPolicy:
            for permission in (True, False):
                result = parse_interaction(chat('你好', '再说一句'), can_act=permission, analysis=policy)
                self.assertEqual(result.messages, ('你好', '再说一句'))
                self.assertIsNone(result.decision)
            self.assertIsNone(parse_interaction(chat(), can_act=False, analysis=policy).decision)
        with self.assertRaises(InvalidResponse):
            parse_interaction(chat(), can_act=True)
        for raw in (turn('Rock', '不能部分公开'), WAIT_RESPONSE,
                    json.dumps({'actions': [{'type': 'ANALYZE', 'text': 'private'}]}),
                    json.dumps(postmatch_payload('early'))):
            with self.subTest(raw=raw), self.assertRaises(InvalidResponse):
                parse_interaction(raw, can_act=False)


class ChatSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_chat_is_immediate_but_pending_commit_is_private(self):
        first_say, answer = asyncio.Event(), asyncio.Event()
        events, records = [], []

        def emit(event):
            events.append(event)
            if event.text == '已结束行动':
                first_say.set()
            if event.text == '回答':
                answer.set()

        async def a_reply(view, feedback):
            if view.can_act:
                return turn('Paper', '已结束行动')
            self.assertEqual(view.own_move, Gesture.PAPER)
            self.assertTrue(view.to_dict()['own_move_locked'])
            self.assertFalse(view.opponent_committed)
            if not view.notifications:
                self.assertIn('action_ended', view.request_reasons)
                return chat()
            self.assertEqual(view.notifications, (2,))
            return chat('回答')

        async def b_reply(view, feedback):
            self.assertFalse(view.opponent_committed)
            self.assertNotIn('PRIVATE-ANALYSIS', repr(view))
            if len(b.views) == 1:
                await first_say.wait()
                self.assertEqual(view.history, ())  # 已在生成的请求不会暗中刷新。
                return chat('提问')
            self.assertEqual([e.text for e in view.history], ['已结束行动', '提问'])
            await answer.wait()
            return turn('Rock')

        a, b = Actor(a_reply), Actor(b_reply)
        state = await asyncio.wait_for(run_game(a, b, policy=FAST, on_event=emit, on_request=records.append), 3)
        self.assertEqual((state.result.winner, state.completed_ticks), ('A', 1))
        self.assertEqual([e.text for e in events if e.kind == 'say'], ['已结束行动', '提问', '回答'])
        self.assertEqual([e.kind for e in state.events[:3]], ['say'] * 3)
        self.assertEqual(len(b.views), 2)
        self.assertEqual(sum(v.can_act for v, _ in a.views), 1)
        starts = [r for r in records if r['kind'] == 'request_start']
        self.assertEqual(len({r['request_id'] for r in starts}), len(a.views) + len(b.views))
        self.assertFalse(any(r['kind'] == 'request_end' and r['status'] == 'rejected' for r in records))
        self.assertTrue(all(r['tick'] == 1 for r in starts))
        self.assertEqual([e['sequence'] for e in b.views[1][0].to_dict()['history']], [1, 2])

    async def test_alternating_inbox_merges_busy_messages_then_prioritizes_action(self):
        chat_started, committed = asyncio.Event(), asyncio.Event()

        async def a_reply(view, feedback):
            if not view.can_act:
                return chat()
            count = len(a.views)
            if count == 1:
                return chat('一', '二')
            if count == 2:
                await chat_started.wait()
                return chat('三')
            if count == 3:
                return chat('四')
            return turn('Paper')

        async def b_reply(view, feedback):
            if not view.can_act:
                self.assertEqual(view.notifications, ())
                self.assertEqual(view.request_reasons, ('game_start',))
                chat_started.set()
                await committed.wait()
                return chat()
            self.assertEqual(view.tick, 2)
            self.assertEqual(view.notifications, (1, 2, 3, 4))
            self.assertEqual(set(view.request_reasons), {'turn_changed', 'action_available', 'opponent_say'})
            self.assertEqual(view.to_dict()['event_cursor'], 5)
            self.assertTrue(view.opponent_committed)
            return turn('Rock')

        a, b = Actor(a_reply), Actor(b_reply)
        state = await asyncio.wait_for(run_game(a, b, policy=FAST, initiative='alternating',
            on_event=lambda e: committed.set() if e.kind == 'commit' and e.player == 'A' else None), 3)
        self.assertEqual(state.turns, (1, 1))
        self.assertEqual(state.result.winner, 'A')
        self.assertEqual([v.can_act for v, _ in b.views], [False, True])
        self.assertTrue(all(v.tick == 1 for v, _ in a.views if v.can_act))
        self.assertEqual([e.text for e in state.events if e.kind == 'say'], ['一', '二', '三', '四'])

    async def test_silence_consumes_notification_without_waking_either_player(self):
        silent, release = asyncio.Event(), asyncio.Event()

        async def a_reply(view, feedback):
            if not view.can_act:
                return chat()
            await release.wait()
            return turn('Paper')

        async def b_reply(view, feedback):
            return turn('Rock') if view.can_act else chat()

        def record(row):
            if row['kind'] == 'interaction_applied' and row['player'] == 'B' and row['silent']:
                silent.set()

        a, b = Actor(a_reply), Actor(b_reply)
        task = asyncio.create_task(run_game(a, b, policy=FAST, initiative='alternating', on_request=record))
        try:
            await asyncio.wait_for(silent.wait(), 1)
            self.assertEqual(len(b.views), 1)
            self.assertFalse(task.done())
            release.set()
            state = await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(state.completed_ticks, 2)
        self.assertEqual(len(b.views), 2)

    async def test_chat_authorization_retries_are_optional_and_do_not_forfeit(self):
        skipped = asyncio.Event()
        events = []

        async def a_reply(view, feedback):
            if not view.can_act:
                return chat()
            await skipped.wait()
            return turn('Paper')

        async def b_reply(view, feedback):
            return turn('Rock', '越权发言') if not view.can_act else turn('Rock')

        def emit(event):
            events.append(event)
            if event.kind == 'chat_error':
                skipped.set()

        a, b = Actor(a_reply), Actor(b_reply)
        state = await asyncio.wait_for(run_game(a, b, policy=FAST, initiative='alternating', on_event=emit), 3)
        self.assertEqual(state.result.winner, 'A')
        self.assertEqual(state.turns, (1, 1))
        self.assertEqual(len(b.views), 4)
        self.assertIs(b.views[0][0], b.views[2][0])
        self.assertIn('聊天权限', b.views[1][1])
        self.assertNotIn('越权发言', repr(events))

    async def test_abort_keeps_already_published_chat_without_revealing_pending_move(self):
        published = asyncio.Event()

        async def failure(view, feedback):
            await published.wait()
            raise PlayerError('请求被拒绝', retryable=False)

        state = await run_game(ScriptedPlayer([turn('Paper', '已公开')]), Actor(failure), policy=FAST,
                               on_event=lambda e: published.set() if e.kind == 'say' else None)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual(state.moves, (None, None))
        self.assertIn('已公开', repr(state.events))
        self.assertFalse(any(e.kind in ('commit', 'reveal') for e in state.events))

    async def test_late_cancelled_chat_is_removed_before_next_game_and_postmatch(self):
        started = {1: asyncio.Event(), 2: asyncio.Event()}
        bodies, cancelled, records = [], [], []

        async def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            ctx = json.loads(body['messages'][-1]['content'])
            if ctx['phase'] == 'chat':
                game = ctx['match']['game']
                started[game].set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(game)
                    content = chat('STALE-RESPONSE')  # 模拟取消竞态中仍返回完整正文的服务。
            elif ctx['phase'] == 'postmatch':
                content = json.dumps(postmatch_payload('复盘'))
            else:
                self.assertNotIn('STALE-RESPONSE', json.dumps(body))
                content = turn('Paper')
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})

        class Peer(ScriptedPlayer):
            def start_game(self, context):
                super().start_game(context)
                self.game = context['game']
                self.asked = False

            async def respond(self, view, feedback=None):
                if not self.asked:
                    self.asked = True
                    return chat('等我一下')
                await started[self.game].wait()
                return turn('Rock')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            a, b = ChatCompletionPlayer(client, OPENAI, 'test-key'), Peer()
            result = await asyncio.wait_for(run_match(lambda: (a, b), config=MatchConfig(3), policy=FAST,
                on_request=lambda game, row: records.append({'game': game, **row})), 4)
            await a.postmatch({'player': 'A', 'match': {'result': 'finished'}})
            self.assertNotIn('STALE-RESPONSE', json.dumps(a._messages))
        self.assertEqual(result.wins, (2, 0))
        self.assertEqual(cancelled, [1, 2])
        self.assertNotIn('STALE-RESPONSE', json.dumps(bodies[-1]))
        self.assertEqual(len([r for r in records if r['kind'] == 'request_discarded']), 2)

    async def test_external_cancellation_closes_action_and_chat_tasks(self):
        started, cancelled = {'A': asyncio.Event(), 'B': asyncio.Event()}, []

        async def a_reply(view, feedback):
            if len(a.views) == 1:
                return chat('开始聊天')
            started['A'].set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append('A')

        async def b_reply(view, feedback):
            self.assertFalse(view.can_act)
            started['B'].set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append('B')

        a, b = Actor(a_reply), Actor(b_reply)
        task = asyncio.create_task(run_game(a, b, policy=FAST, initiative='alternating'))
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started.values())), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(sorted(cancelled), ['A', 'B'])

    async def test_chat_does_not_extend_revisable_settlement_or_deadline(self):
        a = ScriptedPlayer([turn('Rock'), chat('最后聊一句'), turn('Paper')])
        b = ScriptedPlayer([turn('Rock'), WAIT_RESPONSE])
        state = await run_game(a, b, policy=FAST, move_policy='revisable')
        self.assertEqual((state.result.winner, state.completed_ticks), ('A', 2))
        self.assertEqual([v.tick for v, _ in a.requests], [1, 2, 2])
        a = ScriptedPlayer([item for tick in range(1, 21) for item in
                            (chat(f'第 {tick} 次行动前聊天'), turn('Rock') if tick == 20 else WAIT_RESPONSE)])
        b = ScriptedPlayer()
        state = await run_game(a, b, policy=FAST)
        self.assertEqual((state.completed_ticks, state.result.reason), (20, EndReason.FORFEIT))
        self.assertEqual([e.tick for e in state.events if e.kind == 'reminder'], [10, 18, 19])
        self.assertEqual(len([e for e in state.events if e.kind == 'say']), 20)
        self.assertEqual((len(a.requests), len(b.requests)), (40, 20))

    async def test_publishing_log_failure_cancels_peer(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def a_reply(view, feedback):
            await started.wait()
            return chat('无法写入')

        async def b_reply(view, feedback):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        def emit(event):
            if event.kind == 'say':
                raise LogError('日志故障')

        with self.assertRaises(LogError):
            await run_game(Actor(a_reply), Actor(b_reply), policy=FAST, on_event=emit)
        self.assertTrue(cancelled.is_set())


class ChatOpportunityTests(unittest.IsolatedAsyncioTestCase):
    async def test_either_seat_can_open_chat_before_opponent_finishes_first_action(self):
        for first in ('A', 'B'):
            with self.subTest(first=first):
                published = asyncio.Event()
                records = []

                async def reply(view, feedback):
                    if view.can_act:
                        if view.player == first:
                            await published.wait()
                            self.assertEqual(view.history, ())  # 生成中的快照不会刷新。
                        return turn('Paper' if view.player == first else 'Rock')
                    if 'game_start' in view.request_reasons:
                        self.assertNotEqual(view.player, first)
                        self.assertEqual(view.notifications, ())
                        self.assertEqual(view.history, ())
                        return chat('后手主动开口')
                    return chat()

                state = await asyncio.wait_for(run_game(Actor(reply), Actor(reply), policy=FAST,
                    initiative='alternating', first_player=first, on_request=records.append,
                    on_event=lambda e: published.set() if e.kind == 'say' else None), 3)
                self.assertEqual((state.result.winner, state.completed_ticks), (first, 2))
                self.assertEqual(state.events[0].kind, 'say')
                self.assertEqual(state.events[0].text, '后手主动开口')
                self.assertNotEqual(state.events[0].player, first)
                self.assertFalse(any(r['kind'] == 'request_end' and r['status'] == 'rejected' for r in records))

    async def test_no_say_bo_still_offers_chat_at_each_opening_and_turn_change(self):
        records, states = [], []
        result = await run_match(lambda: (ScriptedPlayer([turn('Paper')], repeat_per_game=True),
                                          ScriptedPlayer([turn('Rock')], repeat_per_game=True)),
            config=MatchConfig(3, initiative='alternating'), policy=FAST,
            on_request=lambda game, row: records.append({'game': game, **row}),
            on_end=lambda game, state, wins, draws: states.append(state))
        self.assertEqual(result.wins, (2, 0))
        self.assertFalse(any(e.kind == 'say' for state in states for e in state.events))
        for game, first in ((1, 'A'), (2, 'B')):
            starts = [r for r in records if r['game'] == game and r['kind'] == 'request_start']
            self.assertEqual(len(starts), 4)  # 两次行动和两次主动聊天机会。
            opening = [r for r in starts if 'game_start' in r['state']['request_reasons']]
            changed = [r for r in starts if 'turn_changed' in r['state']['request_reasons']]
            self.assertEqual({r['player'] for r in opening}, {'A', 'B'})
            self.assertEqual({r['player'] for r in changed}, {'A', 'B'})
            self.assertTrue(all(r['state']['can_act'] == (r['player'] == first) for r in opening))
            self.assertTrue(all(r['state']['can_act'] == (r['player'] != first) for r in changed))
            self.assertTrue(all(r['state']['notifications'] == [] for r in starts))
            self.assertEqual({r['tick'] for r in starts}, {1, 2})
            silent = [r for r in records if r['game'] == game and r['kind'] == 'interaction_applied' and r['silent']]
            self.assertEqual(len(silent), 1)
            discarded = [r for r in records if r['game'] == game and r['kind'] == 'request_discarded']
            self.assertEqual(len(discarded), 1)  # 结算不等待或应用同批完成的聊天。
            self.assertEqual(discarded[0]['phase'], 'chat')

    async def test_simultaneous_completed_player_can_speak_or_stay_silent_without_message(self):
        for message in ('我已经决定，轮到你考虑了。', None):
            with self.subTest(message=message):
                applied = asyncio.Event()
                records = []

                async def a_reply(view, feedback):
                    if view.can_act:
                        return turn('Paper')
                    self.assertEqual(view.request_reasons, ('action_ended',))
                    self.assertEqual(view.notifications, ())
                    self.assertEqual(view.own_move, Gesture.PAPER)
                    self.assertFalse(view.opponent_committed)
                    self.assertEqual(view.history, ())
                    return chat(message) if message else chat()

                async def b_reply(view, feedback):
                    self.assertTrue(view.can_act)
                    await applied.wait()
                    self.assertFalse(view.opponent_committed)
                    self.assertEqual(view.history, ())
                    return turn('Rock')

                def record(row):
                    records.append(row)
                    if row['kind'] == 'interaction_applied' and row['phase'] == 'chat':
                        applied.set()

                a, b = Actor(a_reply), Actor(b_reply)
                state = await asyncio.wait_for(run_game(a, b, policy=FAST, on_request=record), 3)
                self.assertEqual((state.result.winner, state.completed_ticks), ('A', 1))
                self.assertEqual((len(a.views), len(b.views)), (2, 1))
                self.assertEqual([e.text for e in state.events if e.kind == 'say'], [message] if message else [])
                self.assertFalse(any(r['kind'] == 'request_end' and r['status'] == 'rejected' for r in records))

    async def test_opening_chat_cannot_upgrade_permission_during_turn_change_or_retry(self):
        started, committed = asyncio.Event(), asyncio.Event()
        records, events = [], []

        async def a_reply(view, feedback):
            if not view.can_act:
                return chat()
            await started.wait()
            return turn('Paper')

        async def b_reply(view, feedback):
            if not view.can_act:
                started.set()
                await committed.wait()
                self.assertEqual(view.tick, 1)
                self.assertFalse(view.opponent_committed)
                return turn('Rock', '越权消息不能发布')
            self.assertEqual(view.tick, 2)
            self.assertTrue(view.opponent_committed)
            self.assertEqual(set(view.request_reasons), {'turn_changed', 'action_available'})
            return turn('Rock')

        def emit(event):
            events.append(event)
            if event.kind == 'commit' and event.player == 'A':
                committed.set()

        a, b = Actor(a_reply), Actor(b_reply)
        state = await asyncio.wait_for(run_game(a, b, policy=FAST, initiative='alternating',
            on_event=emit, on_request=records.append), 3)
        self.assertEqual((state.result.winner, state.turns), ('A', (1, 1)))
        self.assertEqual([v.can_act for v, _ in b.views], [False, False, False, True])
        self.assertIs(b.views[0][0], b.views[2][0])
        self.assertEqual(len([e for e in events if e.kind == 'chat_error']), 1)
        self.assertNotIn('越权消息', repr(state.events))
        self.assertEqual(len([r for r in records if r['kind'] == 'request_start' and r['player'] == 'A']), 2)


if __name__ == '__main__':
    unittest.main()
