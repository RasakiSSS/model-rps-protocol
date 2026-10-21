import asyncio
from contextlib import asynccontextmanager, redirect_stderr
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fixtures import turn_payload
from model_rps.core.game import GameState, InvalidResponse
from model_rps.engine.match import MatchConfig
from model_rps.players.external import ExternalPlayer
from model_rps.room.base import Room
from model_rps.mcp_agent.server import create_server, make_room
from model_rps.players.base import ScriptedPlayer
from model_rps.room.remote import RemoteRoom, RoomConnector

if importlib.util.find_spec('mcp'):
    # SDK stdio 的默认 stderr 在导入时绑定，须在各测试的临时重定向之前导入。
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.001)


def actions(move='Paper'):
    return turn_payload([{'type': 'MOVE', 'move': move}], 'EXTERNAL-ANALYSIS')['actions']


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        output = (Path(self.temp.name) / 'stderr.txt').open('w', encoding='utf-8')
        self.addCleanup(output.close)
        self.stderr = redirect_stderr(output)
        self.stderr.__enter__()
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def room(self, **kwargs):
        peer = ScriptedPlayer([json.dumps(turn_payload(
            [{'type': 'MOVE', 'move': 'Rock'}], 'OPPONENT-PRIVATE'))], repeat_per_game=True)
        self.peer_closed = False

        @asynccontextmanager
        async def opponent():
            try:
                yield peer
            finally:
                self.peer_closed = True

        room = Room(opponent, log_dir=self.temp.name, **kwargs)
        self.addAsyncCleanup(room.close)
        return room

    async def ready(self, room):
        await until(lambda: room.player.can_submit and room.player.can_act)

    async def test_full_bo_privacy_seats_modes_postmatch_and_logs(self):
        for identity in ('A', 'B'):
            for initiative, policy in (('simultaneous', 'locked'), ('simultaneous', 'revisable'),
                                       ('alternating', 'locked')):
                with self.subTest(identity=identity, initiative=initiative, policy=policy):
                    room = self.room(identity=identity, config=MatchConfig(best_of=3,
                        initiative=initiative, move_policy=policy))
                    await room.start()
                    counter = 0
                    async with asyncio.timeout(5):
                        while room.status == 'running':
                            snapshot = room.snapshot()
                            self.assertNotIn('OPPONENT-PRIVATE', repr(snapshot))
                            if snapshot['can_submit'] and snapshot.get('state', {}).get('can_act'):
                                counter += 1
                                result = await room.act(actions(), snapshot['turn_token'], str(counter),
                                                        snapshot['revision'])
                                self.assertTrue(result['accepted'])
                            else:
                                await room.wait(snapshot['revision'], .05)
                    self.assertEqual(room.status, 'postmatch')
                    self.assertEqual(room.result['winner'], identity)
                    self.assertEqual(room.result['games_played'], 2)
                    self.assertIn('OPPONENT-PRIVATE', repr(room.snapshot()))
                    await until(lambda: room.player.can_submit and room.player.phase == 'postmatch')
                    token = room.player.turn_token
                    goodbye = [{'type': 'SAY', 'text': '赛后交流'}, {'type': 'EXIT'}]
                    await room.act(goodbye, token, 'goodbye')
                    await until(lambda: room.status == 'complete')
                    replay = await room.act(goodbye, token, 'goodbye')
                    self.assertTrue(replay['replayed'])
                    self.assertEqual(room.result['games_played'], 2)
                    self.assertEqual((await room.start())['status'], 'complete')
                    self.assertTrue(self.peer_closed)
                    records = (room.logs.path / ('red' if identity == 'A' else 'blue') / 'debug.log').read_text('utf-8')
                    self.assertIn('"source": "mcp"', records)
                    chat = (room.logs.path / 'chat.log').read_text('utf-8')
                    self.assertEqual(chat.count('：赛后交流'), 1)

    async def test_atomic_rejection_idempotency_event_delivery_and_wait_does_not_act(self):
        room = self.room(secrets=('test-secret-value',))
        await room.start()
        await self.ready(room)
        token = room.player.turn_token
        revision = room.revision
        tick = room.player.view.tick
        bad = [{'type': 'SAY', 'text': 'must-not-publish'}, {'type': 'MOVE', 'move': 'Lizard'}]
        with self.assertRaises(InvalidResponse):
            await room.act(bad, token, 'bad')
        self.assertEqual(room.player.turn_token, token)
        self.assertNotIn('must-not-publish', repr(room.events))
        await room.wait(room.revision, .02)
        self.assertEqual(room.player.view.tick, tick)
        self.assertTrue(room.player.can_act)
        # A message received after the caller's last read must survive the act response and retry.
        room._publish('test_public', text='test-secret-value')
        say = [{'type': 'SAY', 'text': 'hello-once'}]
        reply = await room.act(say, token, 'say-1', revision)
        self.assertIn('test_public', repr(reply['state']['events']))
        self.assertNotIn('test-secret-value', repr(reply))
        await until(lambda: room.player.can_submit)
        replay = await room.act(say, token, 'say-1', revision)
        self.assertTrue(replay['replayed'])
        self.assertIn('test_public', repr(replay['state']['events']))
        with self.assertRaises(InvalidResponse):
            await room.act(actions(), room.player.turn_token, 'say-1')
        with self.assertRaises(InvalidResponse):
            await room.act(actions(), token, 'stale')
        events = [e for e in room.events if e.get('event', {}).get('text') == 'hello-once']
        self.assertEqual(len(events), 1)
        self.assertEqual(room.snapshot(room.revision)['events'], [])
        for cursor in (-1, True, room.revision + 1):
            with self.assertRaises(InvalidResponse):
                await room.wait(cursor, 0)
        for timeout in (-1, 21, float('nan')):
            with self.assertRaises(InvalidResponse):
                await room.wait(room.revision, timeout)
        await room.close()
        self.assertEqual(room.status, 'cancelled')
        self.assertIsNone(room.player._future)
        self.assertTrue(self.peer_closed)

    async def test_stale_token_rejected_when_pending_input_changes_tick_or_permission(self):
        player = ExternalPlayer(analysis='optional')
        player.start_game({'game': 1})
        view = replace(GameState().view_for('B'), can_act=False, request_id=7)
        player.update_view(view)
        receive = asyncio.create_task(player.respond(view))
        try:
            await until(lambda: player.can_submit)
            token = player.turn_token
            player.update_view(replace(view, tick=2, can_act=True))
            with self.assertRaises(InvalidResponse):
                player.submit_actions(actions(), token)
            self.assertFalse(receive.done())
            player.submit_actions(actions(), player.turn_token)
            self.assertEqual((await receive).view.tick, 2)
        finally:
            receive.cancel()
            await asyncio.gather(receive, return_exceptions=True)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP optional SDK is not installed')
    async def test_sdk_tool_discovery_and_errors(self):
        room = self.room()
        async with Client(create_server(room)) as client:
            tools = await client.list_tools()
            self.assertEqual({t.name for t in tools.tools}, {'rps_start', 'rps_state', 'rps_act', 'rps_wait'})
            response = await client.call_tool('rps_start', {})
            self.assertEqual(response.structured_content['rules']['best_of'], 1)
            response = await client.call_tool('rps_start', {'room_url': 'http://127.0.0.1:8765', 'token': 'unused'})
            self.assertIn('--shared-room', response.structured_content['error'])
            await self.ready(room)
            response = await client.call_tool('rps_act', {'actions': actions(), 'turn_token': 'old', 'action_id': 'x'})
            self.assertFalse(response.structured_content['accepted'])
            self.assertIn('turn_token', response.structured_content['state'])
            response = await client.call_tool('rps_wait', {'after_revision': -1})
            self.assertIn('error', response.structured_content)
        self.assertEqual(room.status, 'cancelled')

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP optional SDK is not installed')
    async def test_stdio_full_match_uses_json_rpc_and_preserves_stdout(self):
        params = StdioServerParameters(command=sys.executable, args=[
            '-X', 'utf8', '-m', 'model_rps.mcp_agent.server', '--no-config', '--mode', 'demo',
            '--bo', '1', '--log-dir', self.temp.name], cwd=str(Path(__file__).resolve().parents[1]))
        async with Client(params) as client:
            started = await client.call_tool('rps_start', {})
            state = started.structured_content['state']
            for serial in range(60):
                if state['status'] == 'complete':
                    break
                self.assertNotIn(state['status'], ('failed', 'cancelled'))
                if state['can_submit'] and (state['phase'] == 'postmatch' or state['state']['can_act']):
                    commands = [{'type': 'EXIT'}] if state['phase'] == 'postmatch' else actions()
                    reply = await client.call_tool('rps_act', {'actions': commands,
                        'turn_token': state['turn_token'], 'action_id': str(serial), 'after_revision': state['revision']})
                    self.assertTrue(reply.structured_content['accepted'])
                    state = reply.structured_content['state']
                else:
                    reply = await client.call_tool('rps_wait', {'after_revision': state['revision'], 'timeout_seconds': .2})
                    state = reply.structured_content
            self.assertEqual(state['status'], 'complete')
            self.assertEqual(state['result']['games_played'], 1)


class MCPConfigTests(unittest.TestCase):
    def test_shared_entry_does_not_load_secrets_or_use_stale_environment_token(self):
        with patch('model_rps.mcp_agent.server.load_config', side_effect=AssertionError('must not load config')):
            with patch.dict('os.environ', {'RPS_SEAT_TOKEN': 'previous-room-token'}):
                connector = make_room(['--shared-room', '--agent-name', 'DSH'])
                self.assertIsInstance(connector, RoomConnector)
                self.assertFalse(connector.rooms)
                self.assertEqual(connector.default_name, 'DSH')
                fixed = make_room(['--room-url', 'http://127.0.0.1:8765'])
                self.assertIsInstance(fixed, RemoteRoom)
                self.assertEqual(fixed.token, 'previous-room-token')
        with self.assertRaises(ValueError):
            make_room(['--shared-room', '--token', 'not-a-startup-option'])

    def test_external_seat_needs_no_key_and_other_seat_keeps_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_text('[app]\nmode="live"\n[players.blue]\napi_key="test-key"\n', encoding='utf-8')
            room = make_room(['--config', str(config), '--agent-name', 'Codex'])
            self.assertEqual(room.identity, 'A')
            self.assertEqual(room.names['A'], 'Red · Codex')
            self.assertEqual(room.player.analysis_policy, 'required')
            self.assertIn('test-key', room.secrets)
        room = make_room(['--no-config', '--agent-seat', 'Blue', '--agent-analysis', 'off'])
        self.assertEqual(room.identity, 'B')
        self.assertEqual(room.player.analysis_policy, 'off')

    def test_unsupported_terminal_options_and_invalid_names_rejected(self):
        for args in (['--debug'], ['--session'], ['--blue-type', 'human'], ['--agent-name', 'bad\nname']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                make_room(['--no-config', *args])


if __name__ == '__main__':
    unittest.main()
