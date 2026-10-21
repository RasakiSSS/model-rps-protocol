import asyncio
import json
import unittest

import httpx

from fixtures import turn_payload, postmatch_payload
from model_rps.core.game import EndReason, Event, GameState, Gesture, Initiative, InvalidResponse, MovePolicy
from model_rps.players.human import HumanPlayer
from model_rps.core.identities import named_data
from model_rps.engine.match import MatchConfig, run_match
from model_rps.players.base import ChatCompletionPlayer, OPENAI, ScriptedPlayer, PlayerError
from model_rps.engine.postmatch import MatchArchive, run_postmatch
from model_rps.engine.requests import RequestPolicy
from model_rps.engine.scheduler import GameSession
from model_rps.players.seats import Replacement, SeatPlayer
from test_human import until


FAST = RequestPolicy(timeout=2, retry_delay=0)


def action(move=None, analysis='MODEL-ANALYSIS'):
    return json.dumps(turn_payload([{'type': 'MOVE', 'move': move}] if move else [], analysis))


class GatedPlayer(ScriptedPlayer):
    def __init__(self):
        super().__init__()
        self.actions = asyncio.Queue()

    async def respond(self, view, feedback=None):
        if not view.can_act:
            self.chat_requests.append((view, feedback))
            return '{"actions":[]}'
        self.requests.append((view, feedback))
        return await self.actions.get()


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    def arrange(self, *, identity='A', state=None, replacement=None, prepare=None):
        human, opponent = HumanPlayer(), GatedPlayer()
        model = replacement or ScriptedPlayer([action('Paper')])
        prefix = 'Red' if identity == 'A' else 'Blue'
        candidate = Replacement(model, f'{prefix} · 接管模型', 'takeover')
        seat = SeatPlayer(human, f'{prefix} · 小明', prepare_handoff=prepare or (lambda profile: candidate))
        other = SeatPlayer(opponent, ('Blue' if identity == 'A' else 'Red') + ' · 对手')
        players = (seat, other) if identity == 'A' else (other, seat)
        context = {'game': 1, 'wins': {'A': 0, 'B': 0}, 'best_of': 1, 'last_game': None}
        for player in players:
            player.start_match()
            player.start_game(context)
        records = []
        session = GameSession(*players, state=state or GameState(), policy=FAST, on_request=records.append)
        return human, opponent, model, seat, session, records

    async def launch(self, session, human):
        task = asyncio.create_task(session.run())
        self.addAsyncCleanup(self.cancel, task)
        await until(lambda: human._available.is_set())
        return task

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_confirmed_held_action_survives_and_new_model_cannot_act_twice_both_seats(self):
        for identity in ('A', 'B'):
            with self.subTest(identity=identity):
                human, opponent, model, seat, session, records = self.arrange(identity=identity)
                task = await self.launch(session, human)
                await human.submit('/say HUMAN-CHAT')
                await until(lambda: human._available.is_set())
                await human.submit('/analyze HUMAN-SUBMITTED')
                await human.submit('/move Rock')
                old_epoch = human.epoch
                await human.submit('/endturn')
                # 不等待调度接受 END TURN，紧接着发起交接，覆盖确认输入与控制唤醒的竞争。
                await human.submit('/handoff takeover')
                await until(lambda: bool(model.chat_requests))
                self.assertFalse(model.requests)
                view = model.chat_requests[0][0]
                self.assertFalse(view.can_act)
                self.assertEqual(view.own_move, Gesture.ROCK)
                self.assertTrue(view.to_dict()['own_move_locked'])
                self.assertEqual(session.state.completed_ticks, 0)
                self.assertIsNone(session.state.moves[0 if identity == 'A' else 1])
                self.assertEqual(model.handoff['own_decisions'][0]['summary'], 'HUMAN-SUBMITTED')
                self.assertIn('HUMAN-CHAT', json.dumps(model.handoff))
                self.assertIsNone(human._future)
                with self.assertRaises(InvalidResponse):
                    await human.submit('/say 迟到输入', epoch=old_epoch)
                with self.assertRaises(InvalidResponse):
                    await human.submit('/move Paper')
                opponent.actions.put_nowait(action('Paper', 'OPPONENT-PRIVATE'))
                state = await asyncio.wait_for(task, 3)
                self.assertEqual(state.completed_ticks, 1)
                self.assertEqual(state.moves[0 if identity == 'A' else 1], Gesture.ROCK)
                commit = next(e for e in state.events if e.kind == 'commit' and e.player == identity)
                self.assertTrue(commit.actor.endswith('小明'))
                displayed = named_data([event.to_dict() for event in state.events],
                                       {'A': 'Red · 接管模型', 'B': 'Blue · 接管模型'})
                self.assertTrue(next(e for e in displayed if e['text'] == 'HUMAN-CHAT')['player'].endswith('小明'))
                self.assertNotIn('OPPONENT-PRIVATE', json.dumps(model.handoff))
                self.assertEqual(len([r for r in records if r['kind'] == 'decision' and r['player'] == identity]), 1)

    async def test_unsubmitted_drafts_discarded_and_legal_modes_keep_state(self):
        for initiative, policy in ((Initiative.SIMULTANEOUS, MovePolicy.LOCKED),
                                   (Initiative.SIMULTANEOUS, MovePolicy.REVISABLE),
                                   (Initiative.ALTERNATING, MovePolicy.LOCKED)):
            with self.subTest(initiative=initiative, policy=policy):
                state = GameState(move_policy=policy, initiative=initiative, completed_ticks=4, turns=(2, 2))
                human, opponent, model, seat, session, _ = self.arrange(state=state)
                task = await self.launch(session, human)
                await human.submit('/move Scissors')
                await human.submit('/analyze DRAFT-MUST-NOT-TRANSFER')
                await human.submit('/handoff takeover')
                await until(lambda: bool(model.requests))
                first = model.requests[0][0]
                self.assertEqual(first.tick, 5)
                self.assertTrue(first.can_act)
                self.assertEqual(first.move_policy, policy)
                self.assertEqual(first.own_turn, 3 if initiative == Initiative.ALTERNATING else 5)
                self.assertIsNone(first.own_move)
                self.assertNotIn('DRAFT-MUST-NOT-TRANSFER', json.dumps(model.handoff))
                self.assertFalse(human.analysis)
                self.assertIsNone(human.move)
                opponent.actions.put_nowait(action('Rock'))
                if policy == MovePolicy.REVISABLE:
                    opponent.actions.put_nowait(action())
                result = await asyncio.wait_for(task, 3)
                self.assertEqual(result.result.winner, 'A')
                self.assertEqual(result.completed_ticks, 5 if policy == MovePolicy.LOCKED and
                                 initiative == Initiative.SIMULTANEOUS else 6)

    async def test_locked_move_remains_locked_and_alternating_non_actor_only_chats(self):
        state = GameState(completed_ticks=1, moves=(Gesture.ROCK, None), turns=(1, 0),
                          initiative=Initiative.ALTERNATING)
        human, opponent, model, seat, session, _ = self.arrange(state=state)
        task = await self.launch(session, human)
        self.assertFalse(human.can_act)
        await human.submit('/handoff takeover')
        await until(lambda: bool(model.chat_requests))
        self.assertEqual(model.chat_requests[0][0].own_move, Gesture.ROCK)
        self.assertFalse(model.requests)
        opponent.actions.put_nowait(action())
        await until(lambda: bool(model.requests))
        opponent.actions.put_nowait(action('Paper'))
        state = await asyncio.wait_for(task, 3)
        self.assertEqual(state.moves[0], Gesture.ROCK)
        self.assertEqual(state.result.winner, 'B')
        self.assertTrue(any(e.kind == 'move_ignored' and e.actor == 'Red · 接管模型' for e in state.events))

    async def test_invalid_profile_keeps_drafts_human_and_request(self):
        def reject(profile):
            raise ValueError('未配置模型')
        human, opponent, _, seat, session, records = self.arrange(prepare=reject)
        task = await self.launch(session, human)
        await human.submit('/move Paper')
        await human.submit('/analyze KEEP-DRAFT')
        with self.assertRaisesRegex(InvalidResponse, '未配置模型'):
            await human.submit('/handoff missing')
        self.assertIs(seat.player, human)
        self.assertEqual(human.move.move, Gesture.PAPER)
        self.assertTrue(human._available.is_set())
        self.assertFalse(any(r['kind'] == 'handoff' for r in records))
        await human.submit('/endturn')
        opponent.actions.put_nowait(action('Rock'))
        self.assertEqual((await task).result.winner, 'A')

    async def test_api_failure_after_switch_uses_normal_retry_and_does_not_restore_human(self):
        model = ScriptedPlayer([PlayerError('测试超时')] * 3)
        human, opponent, _, seat, session, records = self.arrange(replacement=model)
        task = await self.launch(session, human)
        await human.submit('/handoff takeover')
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual(len(model.requests), 3)
        self.assertIs(seat.player, model)
        self.assertTrue(human.exiting)
        self.assertFalse(session.pending)

    async def test_handoff_at_finished_boundary_rejected_without_pending_control(self):
        human, opponent, model, seat, session, records = self.arrange()
        task = await self.launch(session, human)
        await human.submit('/move Paper')
        await human.submit('/endturn')
        opponent.actions.put_nowait(action('Rock'))
        # 等待全部行动就绪，和控制指令在同一轮唤醒；结束优先于接管。
        await until(lambda: all(r.task.done() for r in session.pending.values()) or task.done())
        with self.assertRaises(InvalidResponse):
            await human.submit('/handoff takeover')
        await task
        self.assertIs(seat.player, human)
        self.assertFalse(model.requests)

    async def test_bo_history_private_analysis_scores_and_postmatch_continue_with_replacement(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            context = json.loads(body['messages'][-1]['content'])
            payload = (postmatch_payload() if context['phase'] == 'postmatch' else
                       {'actions': []} if context['phase'] == 'chat' else turn_payload(
                           [{'type': 'MOVE', 'move': 'Paper'}], 'NEW-MODEL-ANALYSIS'))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                   'message': {'content': json.dumps(payload)}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            human = HumanPlayer()
            names = {'A': 'Red · 小明', 'B': 'Blue · 对手'}
            model = ChatCompletionPlayer(client, OPENAI, 'test-key', names={**names, 'A': 'Red · 新模型'})
            candidate = Replacement(model, 'Red · 新模型', 'new')
            red = SeatPlayer(human, names['A'], prepare_handoff=lambda _: candidate)
            blue = SeatPlayer(ScriptedPlayer([action('Rock', 'OPPONENT-SECRET')], repeat_per_game=True), names['B'])
            archive, events = MatchArchive(), []
            config = MatchConfig(best_of=5)
            task = asyncio.create_task(run_match(lambda: (red, blue), config=config, policy=FAST,
                on_event=lambda n, event: events.append((n, event)), on_request=archive.request,
                on_end=lambda n, state, wins, draws: archive.finish_game(n, state)))
            self.addAsyncCleanup(self.cancel, task)
            await until(lambda: human._available.is_set())
            await human.submit('/say ORIGINAL-CHAT')
            await until(lambda: human._available.is_set())
            await human.submit('/analyze ORIGINAL-ANALYSIS')
            await human.submit('/move Paper')
            await human.submit('/endturn')
            await until(lambda: human.match.get('game') == 2 and human._available.is_set())
            await human.submit('/analyze UNSUBMITTED-DRAFT')
            await human.submit('/handoff new')
            result = await asyncio.wait_for(task, 3)
            self.assertEqual((result.wins, result.games_played), ((3, 0), 3))
            first = bodies[0]
            imported = json.loads(first['messages'][1]['content'])
            self.assertEqual(imported['phase'], 'handoff')
            self.assertEqual(imported['match']['wins']['Red · 新模型'], 1)
            self.assertEqual(imported['games'][0]['game'], 1)
            self.assertIn('ORIGINAL-CHAT', json.dumps(imported))
            self.assertIn('ORIGINAL-ANALYSIS', json.dumps(imported))
            self.assertNotIn('OPPONENT-SECRET', json.dumps(first))
            self.assertNotIn('UNSUBMITTED-DRAFT', json.dumps(first))
            self.assertEqual([m['role'] for m in first['messages']], ['system', 'user', 'user'])
            self.assertEqual(imported['own_decisions'][0]['player'], 'Red · 小明')
            normal = json.loads(first['messages'][-1]['content'])
            self.assertNotIn('last_game', normal['match'])
            self.assertTrue(any(e['kind'] == 'handoff' for e in normal['state']['history']))
            report = archive.report(config, result)
            self.assertEqual(report['games'][0]['decisions'][0]['actor'], 'Red · 小明')
            outcome = await run_postmatch((red, blue), report, policy=FAST)
            self.assertTrue(outcome.complete)
            self.assertEqual(json.loads(bodies[-1]['messages'][-1]['content'])['phase'], 'postmatch')
            self.assertIn('ORIGINAL-ANALYSIS', json.dumps(bodies[-1]))
            self.assertIn('OPPONENT-SECRET', json.dumps(bodies[-1]))
            # 新 BO 清空导入资料，与普通模型历史遵守同一生命周期。
            red.start_match()
            self.assertEqual(len(model._messages), 1)
            self.assertEqual(red.games, [])
            self.assertEqual(red.decisions, [])

    async def test_cancelled_match_closes_queued_handoff_and_input(self):
        human, opponent, model, seat, session, records = self.arrange()
        task = await self.launch(session, human)
        handoff = asyncio.create_task(human.submit('/handoff takeover'))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.assertRaises(InvalidResponse):
            await asyncio.wait_for(handoff, 1)
        self.assertFalse(session.pending)
        self.assertIsNone(human._future)
