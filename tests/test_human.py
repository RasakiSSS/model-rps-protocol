import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fixtures import turn_payload
from model_rps.cli import make_parser
from model_rps.core.commands import AnalysisPolicy
from model_rps.config import RuntimeConfig, load_config
from model_rps.core.game import GameState, Gesture, InvalidResponse, MovePolicy
from model_rps.players.human import HumanPlayer
from model_rps.player_factory import load_keys, player_keys
from model_rps.players.base import ScriptedPlayer
from model_rps.engine.postmatch import run_postmatch
from model_rps.engine.runner import RequestPolicy, run_game


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def move(gesture):
    return json.dumps(turn_payload([{'type': 'MOVE', 'move': gesture}], 'MODEL-PRIVATE'))


class HumanConfigTests(unittest.TestCase):
    def test_type_defaults_precedence_and_only_model_key_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[players.red]\ntype="human"\nname="小明"\napi_key=""\n'
                            '[players.blue]\napi_key="blue-test-key"', encoding='utf-8')
            args = make_parser(load_config(path)).parse_args([])
            settings = RuntimeConfig.from_args(args)
            self.assertEqual((settings.red.type, settings.red.display_name, settings.red.analysis),
                             ('human', '小明', 'optional'))
            self.assertEqual((settings.blue.type, settings.blue.analysis), ('llm', 'required'))
            self.assertEqual(player_keys(settings, Path('missing.py')), (None, 'blue-test-key'))
            args = make_parser(load_config(path)).parse_args(['--red-type', 'llm', '--red-analysis', 'off', '--mode', 'demo'])
            self.assertEqual(RuntimeConfig.from_args(args).red.analysis, 'off')
            path.write_text('DEEPSEEK_API_KEY="only-needed"', encoding='utf-8')
            self.assertEqual(load_keys(path, required=('deepseek',)), ('only-needed', None))

    def test_invalid_type_two_humans_and_explicit_analysis(self):
        with self.assertRaises(ValueError):
            RuntimeConfig.from_args(make_parser().parse_args(['--red-type', 'human', '--blue-type', 'human']))
        settings = RuntimeConfig.from_args(make_parser().parse_args(['--blue-type', 'human', '--blue-analysis', 'required']))
        self.assertEqual((settings.blue.display_name, settings.blue.analysis), ('玩家', 'required'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.toml'
            for value in ('"robot"', 'true', '1'):
                path.write_text('[players.red]\ntype=' + value, encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_config(path)


class HumanPlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_drafts_survive_chat_replace_move_and_share_validation(self):
        player = HumanPlayer(analysis='required')
        player.start_game({'game': 1})
        view = GameState().view_for('A')
        player.update_view(view)
        receiver = asyncio.create_task(player.respond(view))
        await until(lambda: player._available.is_set())
        await player.submit('/move Rock')
        await player.submit('/move Paper')
        await player.submit('/analyze HUMAN-PRIVATE')
        await player.submit('/say 聊天不提交草稿')
        said = await receiver
        self.assertEqual(json.loads(said.content), {'actions': [{'type': 'SAY', 'text': '聊天不提交草稿'}]})
        self.assertEqual(player.move.move, Gesture.PAPER)
        self.assertEqual(len(player.analysis), 1)
        receiver = asyncio.create_task(player.respond(view))
        await until(lambda: player._available.is_set())
        await player.submit('/endturn')
        submitted = await receiver
        actions = json.loads(submitted.content)['actions']
        self.assertEqual([a['type'] for a in actions], ['ANALYZE', 'MOVE', 'ENDTURN'])
        self.assertEqual(actions[1]['move'], 'Paper')
        self.assertIsNone(player.move)
        self.assertFalse(player.analysis)
        self.assertFalse(player.can_act)

    async def test_local_errors_do_not_use_retries_and_draft_clear_or_new_game(self):
        player = HumanPlayer(analysis='required')
        player.start_game({'game': 1})
        view = GameState().view_for('A')
        player.update_view(view)
        receiver = asyncio.create_task(player.respond(view))
        await until(lambda: player._available.is_set())
        for command in ['/move Lizard', '/exit', '/endturn', '/endturn', '/endturn', '/say']:
            with self.subTest(command=command), self.assertRaises(InvalidResponse):
                await player.submit(command)
            self.assertFalse(receiver.done())
        await player.submit('/move Rock')
        await player.submit('/analyze secret')
        await player.submit('/clear')
        self.assertIsNone(player.move)
        self.assertEqual(player.analysis, [])
        await player.submit('/move Paper')
        epoch = player.epoch
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        player.start_game({'game': 2})
        self.assertIsNone(player.move)
        self.assertEqual(player.analysis, [])
        with self.assertRaises(InvalidResponse):
            await player.submit('/endturn', epoch=epoch)

    async def test_human_plays_either_seat_all_modes_and_waiting_has_no_api_timeout(self):
        for identity in ('A', 'B'):
            for initiative, rule in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
                with self.subTest(identity=identity, initiative=initiative, rule=rule):
                    human = HumanPlayer()
                    human.start_game({'game': 1})
                    model = ScriptedPlayer([move('Rock'), move('Rock')])
                    players = (human, model) if identity == 'A' else (model, human)
                    events, records = [], []
                    task = asyncio.create_task(run_game(*players, initiative=initiative, move_policy=rule,
                        policy=RequestPolicy(.02, 0), on_event=events.append, on_request=records.append))
                    try:
                        await until(lambda: human.can_act and human._available.is_set())
                        await asyncio.sleep(.04)
                        self.assertFalse(task.done())
                        if initiative == 'simultaneous':
                            self.assertFalse(human.view.opponent_committed)
                        else:
                            self.assertEqual(human.view.opponent_committed, identity == 'B')
                        await human.submit('/analyze HUMAN-PRIVATE')
                        await human.submit('/move Paper')
                        await human.submit('/say 提交前先聊一句')
                        await until(lambda: human._available.is_set())
                        self.assertTrue(any(e.kind == 'say' and e.player == identity for e in events))
                        self.assertFalse(any(e.player == identity and e.kind == 'commit' for e in events))
                        self.assertNotIn('HUMAN-PRIVATE', repr(events))
                        await human.submit('/endturn')
                        if rule == 'revisable':
                            await until(lambda: human.can_act and human.view.tick == 2 and human._available.is_set())
                            await human.submit('/endturn')  # 保留原拳型。
                        state = await asyncio.wait_for(task, 3)
                        self.assertEqual(state.result.winner, identity)
                        self.assertTrue(any(r['kind'] == 'decision' and r['player'] == identity for r in records))
                        self.assertFalse(any(r['kind'] == 'request_end' and r['status'] == 'rejected' for r in records))
                        self.assertIsNone(human._future)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_nonactor_can_chat_but_cannot_draft_and_later_gets_action_without_reply(self):
        human = HumanPlayer()
        human.start_game({'game': 1})
        view = replace(GameState().view_for('B'), can_act=False)
        human.update_view(view)
        receiver = asyncio.create_task(human.respond(view))
        await until(lambda: human._available.is_set())
        for text in ('/move Rock', '/analyze secret', '/endturn'):
            with self.assertRaises(InvalidResponse):
                await human.submit(text)
        human.update_view(replace(view, tick=2, can_act=True))
        await human.submit('/move Scissors')
        await human.submit('/endturn')
        self.assertEqual((await receiver).view.tick, 2)

    async def test_locked_move_is_ignored_by_same_judge(self):
        human = HumanPlayer()
        human.start_game({'game': 1})
        human.update_view(replace(GameState().view_for('A'), own_move=Gesture.ROCK))
        receiver = asyncio.create_task(human.respond(human.view))
        await until(lambda: human._available.is_set())
        self.assertIn('忽略', await human.submit('/move Paper'))
        await human.submit('/endturn')
        self.assertEqual((await receiver).view.own_move, Gesture.ROCK)

    async def test_postmatch_human_can_keep_speaking_and_exit_while_model_is_busy(self):
        human = HumanPlayer()
        release = asyncio.Event()

        class Peer:
            async def postmatch(self, context, feedback=None):
                await release.wait()
                return '{"actions":[{"type":"EXIT"}]}'

        events = []
        task = asyncio.create_task(run_postmatch((human, Peer()), {'games': []},
                                                policy=RequestPolicy(2, 0), on_event=events.append))
        try:
            await until(lambda: human._available.is_set())
            await human.submit('/say 第一句')
            await until(lambda: human._available.is_set())
            await human.submit('/say 不必等对方回复')
            await until(lambda: human._available.is_set())
            await human.submit('/exit')
            await until(lambda: any(e['kind'] == 'exit' for e in events))
            self.assertFalse(task.done())
            self.assertEqual([e['kind'] for e in events], ['say', 'say', 'exit'])
            release.set()
            self.assertTrue((await asyncio.wait_for(task, 2)).complete)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_postmatch_human_waits_chats_and_exits_after_peer_without_timeout(self):
        human = HumanPlayer()

        class Peer:
            async def postmatch(self, context, feedback=None):
                return '{"actions":[{"type":"SAY","text":"再见"},{"type":"EXIT"}]}'

        events = []
        task = asyncio.create_task(run_postmatch((human, Peer()), {'games': []},
                                                policy=RequestPolicy(.01, 0), on_event=events.append))
        try:
            await until(lambda: human.phase == 'postmatch' and human._available.is_set())
            await asyncio.sleep(.04)
            self.assertFalse(task.done())
            self.assertTrue(human.postmatch_context['exited']['B'])
            await human.submit('/say 我还想补充一句')
            await until(lambda: human._available.is_set())
            await human.submit('/exit')
            outcome = await asyncio.wait_for(task, 2)
            self.assertTrue(outcome.complete)
            self.assertEqual([e['kind'] for e in events if e['player'] == 'A'], ['say', 'exit'])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
