import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from model_rps.game import InvalidResponse
from model_rps.match import MatchConfig
from model_rps.room.remote import RemoteRoom
from model_rps.room.terminal import RoomTerminal
from test_shared_room import SharedRoomFixtures, action, until

if importlib.util.find_spec('mcp'):
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters


class RoomLifecycleTests(SharedRoomFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_stop_wakes_old_wait_and_restart_rejects_every_old_request(self):
        room, server = await self.server()
        red, initial = await self.client(room, server, 'A')
        with self.assertRaisesRegex(InvalidResponse, '/stop'):
            await server.service.start()
        entered = asyncio.Event()
        original = room.room.wait
        async def wait(*args, **kwargs):
            entered.set()
            return await original(*args, **kwargs)
        with patch.object(room.room, 'wait', wait):
            pending = asyncio.create_task(red.wait(initial['revision'], 20))
            await asyncio.wait_for(entered.wait(), 3)
            await server.service.stop()
            new = await server.service.start()
            old_result = await asyncio.wait_for(pending, 3)
        self.assertEqual(old_result['status'], 'cancelled')
        self.assertEqual(old_result['room_id'], room.room_id)
        self.assertEqual(room.tokens, new.tokens)
        self.assertNotEqual(room.room_id, new.room_id)
        new_red, _ = await self.client(new, server, 'A')
        new_blue, _ = await self.client(new, server, 'B')
        await self.ready(new_red)
        await self.ready(new_blue)
        for method, params in (
            ('join', {'name': 'old', 'kind': 'human'}), ('state', {'after_revision': 99999}),
            ('wait', {'after_revision': 0}), ('ping', {}), ('leave', {}),
            ('act', {'actions': [], 'turn_token': 'old', 'action_id': '1'}),
        ):
            with self.subTest(method=method), self.assertRaisesRegex(InvalidResponse, '场次'):
                await red._call(method, **params)
        await red.close()  # 旧连接关闭不能取消新比赛。
        self.assertEqual(new.room.status, 'running')
        async with httpx.AsyncClient(trust_env=False) as raw:
            response = await raw.post(server.url + '/stop',
                headers={'Authorization': 'Bearer ' + new.tokens['A']},
                json={'client_id': new_red.client_id, 'room_id': new.room_id})
            self.assertEqual(response.status_code, 404)

    async def test_two_completed_matches_reuse_invites_but_not_history_or_logs(self):
        room, server = await self.server(config=MatchConfig(best_of=1))
        red, _ = await self.client(room, server, 'A', name='first')
        blue, _ = await self.client(room, server, 'B')
        r = await self.ready(red)
        await red.act([{'type': 'SAY', 'text': 'first-match-only'}], r['turn_token'], 'chat')
        await asyncio.gather(self.drive(red, 'Paper'), self.drive(blue, 'Rock'))
        self.assertTrue(server.service.idle)
        log_path = room.room.logs.path
        old_chat = (log_path / 'chat.log').read_text('utf-8')
        await server.service.stop()  # 已完成的比赛不改为 cancelled。
        self.assertEqual(room.room.status, 'complete')
        new = await server.service.start()
        self.assertEqual(new.room.config, room.room.config)
        self.assertFalse(new.clients)
        self.assertFalse(new.room.receipts)
        self.assertFalse(new.room.events)
        self.assertIsNone(new.room.result)
        r2, initial = await self.client(new, server, 'A', name='second')
        b2, _ = await self.client(new, server, 'B')
        self.assertNotIn('first', repr(initial))
        self.assertIsNone(initial['turn_token'])
        result = await asyncio.gather(self.drive(r2, 'Rock'), self.drive(b2, 'Paper'))
        self.assertEqual(result[0]['result']['winner'], 'Blue · 玩家')
        self.assertEqual(result[0]['result']['games_played'], 1)
        self.assertNotEqual(log_path, new.room.logs.path)
        self.assertEqual(old_chat, (log_path / 'chat.log').read_text('utf-8'))
        self.assertNotIn('first-match-only', (new.room.logs.path / 'chat.log').read_text('utf-8'))

    async def test_discovery_race_does_not_silently_join_new_match(self):
        room, server = await self.server()
        candidate = RemoteRoom(server.url, room.tokens['A'])
        self.stack.push_async_callback(candidate.close)
        await candidate.prepare()
        self.assertFalse(room.clients)
        await server.service.stop()
        new = await server.service.start()
        with self.assertRaisesRegex(InvalidResponse, '场次'):
            await candidate.start()
        self.assertFalse(new.clients)
        await self.client(new, server, 'A')

    async def test_stop_running_and_postmatch_preserves_results_and_releases_inputs(self):
        for postmatch in (False, True):
            room, server = await self.server(config=MatchConfig(best_of=1))
            red, _ = await self.client(room, server, 'A')
            blue, _ = await self.client(room, server, 'B')
            if postmatch:
                r, b = await asyncio.gather(self.ready(red), self.ready(blue))
                await asyncio.gather(red.act(action('Paper'), r['turn_token'], 'move'),
                                     blue.act(action('Rock'), b['turn_token'], 'move'))
                await until(lambda: room.room.status == 'postmatch')
            else:
                await self.ready(red)
            with self.assertRaises(InvalidResponse):
                await server.service.start()
            await server.service.stop()
            state = await red.read()
            self.assertEqual(state['status'], 'cancelled')
            self.assertFalse(state['can_submit'])
            self.assertIsNone(state['turn_token'])
            self.assertTrue(all(p._future is None for p in room.room.inputs.values()))
            if postmatch:
                self.assertEqual(state['result']['winner'], 'Red · 玩家')
            new = await server.service.start()
            self.assertEqual(new.tokens, room.tokens)

    async def test_admin_terminal_commands_and_editing_survive_room_output(self):
        room, server = await self.server()
        output = []
        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput(), erase_when_done=True)
            terminal = RoomTerminal(server, session=session, output=output.append)
            task = asyncio.create_task(terminal.run())
            try:
                await until(lambda: session.app.is_running)
                pipe.send_text('/sta')
                await until(lambda: session.default_buffer.text == '/sta')
                red, _ = await self.client(room, server, 'A')
                room.room.output('System：背景输出')
                self.assertEqual(session.default_buffer.text, '/sta')
                pipe.send_text('tus\r')
                await until(lambda: any('Red · 玩家（已加入）' in line for line in output))
                pipe.send_text('/start\r')
                await until(lambda: any('请先 /stop' in line for line in output))
                self.assertEqual(server.service.number, 1)
                pipe.send_text('/stop\r')
                await until(lambda: server.service.idle)
                pipe.send_text('/start\r')
                await until(lambda: server.service.number == 2)
                self.assertEqual(server.service.current.tokens, room.tokens)
                self.assertFalse(server.service.current.clients)
                pipe.send_text('/move Rock\r/help\r')
                await until(lambda: any('未知管理指令' in line for line in output))
                pipe.send_text('/quit\r')
                await asyncio.wait_for(task, 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP optional SDK is not installed')
    async def test_one_stdio_connection_two_players_two_matches_same_tokens(self):
        room, server = await self.server(config=MatchConfig(best_of=1))
        params = StdioServerParameters(command=sys.executable, args=[
            '-X', 'utf8', '-m', 'model_rps.mcp_agent.server', '--shared-room'],
            cwd=str(Path(__file__).resolve().parents[1]))
        async with Client(params) as client:
            async def play(seat, name):
                result = (await client.call_tool('rps_start', {
                    'room_url': server.url, 'token': room.tokens[seat], 'name': name})).structured_content
                state = result['state']
                player_id = state['player_id']
                serial = 0
                async with asyncio.timeout(15):
                    while state['status'] != 'complete':
                        self.assertNotIn(state['status'], ('failed', 'cancelled'))
                        if state['can_submit'] and (state['phase'] == 'postmatch' or state.get('state', {}).get('can_act')):
                            serial += 1
                            commands = [{'type': 'EXIT'}] if state['phase'] == 'postmatch' else action('Paper' if seat == 'A' else 'Rock')
                            result = (await client.call_tool('rps_act', {'actions': commands, 'turn_token': state['turn_token'],
                                'action_id': str(serial), 'player_id': player_id, 'after_revision': state['revision']})).structured_content
                            self.assertTrue(result['accepted'])
                            state = result['state']
                        else:
                            state = (await client.call_tool('rps_wait', {'after_revision': state['revision'],
                                'timeout_seconds': .1, 'player_id': player_id})).structured_content
                return state
            first = await asyncio.gather(play('A', 'first'), play('B', 'first'))
            new = await server.service.start()
            second = await asyncio.gather(play('A', 'second'), play('B', 'second'))
            self.assertEqual({s['room_id'] for s in second}, {new.room_id})
            self.assertTrue({s['player_id'] for s in first}.isdisjoint({s['player_id'] for s in second}))
            for tool, params in (('rps_start', {}), ('rps_state', {}), ('rps_wait', {'after_revision': 0}),
                                  ('rps_act', {'actions': [], 'turn_token': 'old', 'action_id': '1'})):
                result = (await client.call_tool(tool, {**params, 'player_id': first[0]['player_id']})).structured_content
                self.assertIn('场次', result['error'])
                self.assertNotIn('state', result)
                self.assertNotIn(new.room_id, repr(result))
            self.assertEqual(second[0]['result']['winner'], 'Red · second')


if __name__ == '__main__':
    unittest.main()
