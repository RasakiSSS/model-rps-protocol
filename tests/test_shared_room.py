import asyncio
from contextlib import AsyncExitStack
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from model_rps.console import Console
from model_rps.game import InvalidResponse
from model_rps.human_terminal import HumanTerminal
from model_rps.match import MatchConfig
from model_rps.mcp_agent.server import create_server
from model_rps.room.remote import RemoteRoom, RoomConnector
from model_rps.room.client import RemoteHumanPlayer, result_exit_code
from model_rps.room.server import RoomHTTPServer
from model_rps.room.shared import SharedRoom

if importlib.util.find_spec('mcp'):
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(.005)


def action(move):
    return [{'type': 'ANALYZE', 'text': 'private-' + move},
            {'type': 'MOVE', 'move': move}, {'type': 'ENDTURN'}]


class SharedRoomFixtures:
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)

    async def server(self, **kwargs):
        room = SharedRoom(log_dir=self.temp.name, output=lambda _: None, **kwargs)
        server = await self.stack.enter_async_context(RoomHTTPServer(room, port=0))
        return room, server

    async def client(self, room, server, identity, **kwargs):
        client = RemoteRoom(server.url, room.tokens[identity], **kwargs)
        self.stack.push_async_callback(client.close)
        initial = await client.start()
        return client, initial

    async def ready(self, client):
        async with asyncio.timeout(8):
            state = await client.read()
            while not state['can_submit']:
                state = await client.wait(state['revision'], .1)
            return state

    def human(self, remote, initial):
        console = Console({'A': 'Red', 'B': 'Blue'}, color=False)
        output = []
        console.output = output.append
        player = RemoteHumanPlayer(remote, console)
        player.update(initial)
        return player, output

    def observe(self, player):
        task = asyncio.create_task(player.observe())
        async def cancel():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.stack.push_async_callback(cancel)
        return task

    async def drive(self, client, move):
        async with asyncio.timeout(15):
            state = await client.read()
            serial = 0
            while state['status'] != 'complete':
                self.assertNotIn(state['status'], ('failed', 'cancelled'))
                if state['can_submit'] and (state['phase'] == 'postmatch' or state.get('state', {}).get('can_act')):
                    serial += 1
                    commands = [{'type': 'EXIT'}] if state['phase'] == 'postmatch' else action(move)
                    reply = await client.act(commands, state['turn_token'], str(serial), state['revision'])
                    self.assertTrue(reply['accepted'])
                    state = reply['state']
                else:
                    state = await client.wait(state['revision'], .1)
            return state

    def connector(self):
        connector = RoomConnector()
        self.stack.push_async_callback(connector.close)
        return connector


class SharedRoomTests(SharedRoomFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_two_clients_all_modes_bo3_and_seat_scoped_ids(self):
        for initiative, policy in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
            with self.subTest(initiative=initiative, policy=policy):
                room, server = await self.server(config=MatchConfig(best_of=3, initiative=initiative, move_policy=policy))
                red, initial = await self.client(room, server, 'A', name='同名玩家')
                self.assertEqual(initial['status'], 'not_started')
                self.assertFalse(initial['can_submit'])
                blue, _ = await self.client(room, server, 'B', name='同名玩家')
                results = await asyncio.gather(self.drive(red, 'Paper'), self.drive(blue, 'Rock'))
                for state in results:
                    self.assertEqual(state['result']['winner'], 'Red · 同名玩家')
                    self.assertEqual(state['result']['games_played'], 2)
                self.assertIn(('A', '1'), room.room.receipts)
                self.assertIn(('B', '1'), room.room.receipts)

    async def test_auth_privacy_stale_tokens_and_atomic_rejection(self):
        room, server = await self.server()
        red, _ = await self.client(room, server, 'A', name='red')
        blue, _ = await self.client(room, server, 'B', name='blue')
        r, b = await asyncio.gather(self.ready(red), self.ready(blue))
        async with httpx.AsyncClient(trust_env=False) as raw:
            bad = await raw.post(server.url + '/state', json={'client_id': red.client_id})
            self.assertEqual(bad.status_code, 401)
            bad = await raw.post(server.url + '/state', headers={b'Authorization': b'Bearer caf\xe9'},
                                 json={'client_id': red.client_id})
            self.assertEqual(bad.status_code, 401)
        duplicate = RemoteRoom(server.url, room.tokens['A'])
        self.stack.push_async_callback(duplicate.close)
        with self.assertRaises(InvalidResponse):
            await duplicate.start()
        with self.assertRaises(InvalidResponse):
            await blue.act(action('Rock'), r['turn_token'], 'wrong-seat')
        with self.assertRaises(InvalidResponse):
            await red._call('act', actions=action('Paper'), turn_token=r['turn_token'], action_id='override', identity='B')
        with self.assertRaises(InvalidResponse):
            await red.act([{'type': 'SAY', 'text': 'never-published'}, {'type': 'MOVE', 'move': 'invalid'}], r['turn_token'], 'bad')
        self.assertNotIn('never-published', repr(await blue.read()))
        await red.act(action('Paper'), r['turn_token'], 'private')
        await until(lambda: room.room.inputs['A'].view.own_move is not None)
        hidden = await blue.read()
        self.assertNotIn('private-Paper', repr(hidden))
        self.assertNotIn('Paper', repr(hidden))
        self.assertNotIn(room.tokens['A'], repr(hidden))
        replay = await red.act(action('Paper'), r['turn_token'], 'private')
        self.assertTrue(replay['replayed'])
        with self.assertRaises(InvalidResponse):
            await red.act(action('Paper'), r['turn_token'], 'stale')
        results = await asyncio.gather(self.drive(red, 'Paper'), self.drive(blue, 'Rock'))
        self.assertIn('private-Paper', repr(await blue.read()))
        self.assertEqual(results[0]['status'], 'complete')

    async def test_disconnect_and_expired_lease_abort_without_forfeit(self):
        for explicit in (True, False):
            room, server = await self.server()
            red, _ = await self.client(room, server, 'A')
            blue, _ = await self.client(room, server, 'B')
            await self.ready(red)
            if explicit:
                await red.close()
            else:
                room.disconnect_timeout = .15
            await until(lambda: room.room.status == 'cancelled')
            state = await blue.read()
            self.assertIn('不计作弃权', state['error'])
            self.assertIsNone(room.room.result)

    async def test_two_human_terminals_chat_drafts_and_postmatch(self):
        room, server = await self.server()
        red, first = await self.client(room, server, 'A', name='甲')
        blue, second = await self.client(room, server, 'B', name='乙')
        players, terminals, pipes, outputs = [], [], [], []
        for index, (remote, initial) in enumerate(((red, first), (blue, second))):
            console = Console({'A': 'Red · 甲', 'B': 'Blue · 乙'}, color=False)
            lines = []
            console.output = lines.append
            player = RemoteHumanPlayer(remote, console)
            player.update(initial)
            pipe = self.stack.enter_context(create_pipe_input())
            session = PromptSession(input=pipe, output=DummyOutput())
            terminal = HumanTerminal(player, 'A' if index == 0 else 'B', console, session=session)
            task = asyncio.create_task(terminal.run(player.observe()))
            async def cancel(task=task):
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
                except TimeoutError:
                    pass
            self.stack.push_async_callback(cancel)
            players.append(player)
            terminals.append((terminal, task))
            pipes.append(pipe)
            outputs.append(lines)
        await until(lambda: all(p.state.get('state', {}).get('can_act') for p in players))
        await until(lambda: all(t.session.app.is_running for t, _ in terminals))
        pipes[1].send_text('/say 尚未发送')
        await until(lambda: terminals[1][0].session.default_buffer.text == '/say 尚未发送')
        pipes[0].send_text('/say 即时聊天\r')
        await until(lambda: any('即时聊天' in line for line in outputs[1]))
        self.assertEqual(terminals[1][0].session.default_buffer.text, '/say 尚未发送')
        self.assertNotIn('尚未发送', repr(room.room.events))
        pipes[1].send_text('\r')
        await until(lambda: any('尚未发送' in line for line in outputs[0]))
        for index, move in enumerate(('Paper', 'Rock')):
            pipes[index].send_text('/analyze local-secret-' + move + '\r/move ' + move + '\r')
        await until(lambda: all(p.move is not None and p.analysis for p in players))
        self.assertNotIn('local-secret', repr(room.room.events))
        for pipe in pipes:
            pipe.send_text('/endturn\r')
        await until(lambda: all(p.state['phase'] == 'postmatch' for p in players))
        self.assertTrue(all(p.move is None and not p.analysis for p in players))
        await until(lambda: any('local-secret-Rock' in line for line in outputs[0]))
        pipes[0].send_text('/say 赛后再见\r')
        await until(lambda: any('赛后再见' in line for line in outputs[1]))
        for pipe in pipes:
            pipe.send_text('/exit\r')
        async with asyncio.timeout(8):
            self.assertEqual(await asyncio.gather(*(task for _, task in terminals)), ['complete', 'complete'])
        self.assertEqual(room.room.status, 'complete')

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP optional SDK is not installed')
    async def test_human_and_mcp_either_seat_permissions_analysis_and_early_exit(self):
        for human_seat in ('A', 'B'):
            with self.subTest(human_seat=human_seat):
                agent_seat = 'B' if human_seat == 'A' else 'A'
                analysis = ('off', 'required') if human_seat == 'A' else ('required', 'off')
                room, server = await self.server(config=MatchConfig(best_of=3, initiative='alternating'),
                                                 analysis=analysis)
                remote, initial = await self.client(room, server, human_seat, name='人类')
                human, output = self.human(remote, initial)
                observation = self.observe(human)
                bridge = RemoteRoom(server.url, room.tokens[agent_seat], name='Agent', kind='mcp')
                async with Client(create_server(bridge)) as agent:
                    started = (await agent.call_tool('rps_start', {})).structured_content
                    self.assertEqual(started['rules']['best_of'], 3)
                    await self.ready(remote)
                    await until(lambda: human.state['status'] == 'running')
                    initial_agent = (await agent.call_tool('rps_state', {})).structured_content
                    if human_seat == 'B':
                        with self.assertRaisesRegex(InvalidResponse, '没有行动权限'):
                            await human.submit('/move Paper')
                        await human.submit('/say 旁观行动时仍可聊天')
                    else:
                        self.assertFalse(initial_agent['state']['can_act'])
                        rejected = (await agent.call_tool('rps_act', {'actions': action('Rock'),
                            'turn_token': initial_agent['turn_token'], 'action_id': 'out-of-turn'})).structured_content
                        self.assertFalse(rejected['accepted'])
                        accepted = (await agent.call_tool('rps_act', {
                            'actions': [{'type': 'SAY', 'text': '旁观行动时仍可聊天'}],
                            'turn_token': initial_agent['turn_token'], 'action_id': 'chat'})).structured_content
                        self.assertTrue(accepted['accepted'])
                    checked_human = checked_agent = False
                    serial = 0
                    async with asyncio.timeout(15):
                        while room.room.status == 'running':
                            human.update(await remote.read(human.state['revision']))
                            state = (await agent.call_tool('rps_state', {})).structured_content
                            if human.state['can_submit'] and human.state.get('state', {}).get('can_act'):
                                if not checked_human:
                                    with self.assertRaisesRegex(InvalidResponse, '关闭分析'):
                                        await human.submit('/analyze 不应提交')
                                    checked_human = True
                                await human.submit('/move Paper')
                                await human.submit('/endturn')
                            elif state['can_submit'] and state.get('state', {}).get('can_act'):
                                serial += 1
                                if not checked_agent:
                                    rejected = (await agent.call_tool('rps_act', {'actions': action('Rock')[1:],
                                        'turn_token': state['turn_token'], 'action_id': 'missing-analysis'})).structured_content
                                    self.assertFalse(rejected['accepted'])
                                    self.assertIn('策略分析', rejected['error'])
                                    checked_agent = True
                                result = (await agent.call_tool('rps_act', {'actions': action('Rock'),
                                    'turn_token': state['turn_token'], 'action_id': str(serial)})).structured_content
                                self.assertTrue(result['accepted'])
                            else:
                                await asyncio.sleep(.005)
                    await until(lambda: human.state['phase'] == 'postmatch')
                    self.assertEqual(human.state['result']['winner'], f"{'Red' if human_seat == 'A' else 'Blue'} · 人类")
                    self.assertEqual(human.state['result']['games_played'], 2)
                    self.assertTrue(checked_human and checked_agent)
                    state = await self.ready(bridge)
                    result = (await agent.call_tool('rps_act', {'actions': [{'type': 'EXIT'}],
                        'turn_token': state['turn_token'], 'action_id': 'exit'})).structured_content
                    self.assertTrue(result['accepted'])
                # MCP 连接已经关闭，先 EXIT 的席位不能使仍在聊天的人类被中止。
                self.assertEqual(room.room.status, 'postmatch')
                await human.submit('/say 谢谢，最后一句')
                await human.submit('/exit')
                self.assertEqual(await asyncio.wait_for(observation, 5), 'complete')
                self.assertEqual(result_exit_code(human.state), 0)
                self.assertTrue(any('赢得 BO3' in line for line in output))
                self.assertTrue(any('private-Rock' in line for line in output))
                for seat, expected in ((human_seat, 'human'), (agent_seat, 'mcp')):
                    records = [json.loads(line) for line in (room.room.logs.path /
                        ('red' if seat == 'A' else 'blue') / 'debug.log').read_text('utf-8').splitlines()]
                    starts = [r for r in records if r['kind'] == 'request_start']
                    self.assertTrue(any(r.get('source') == expected for r in starts))
                    self.assertTrue(any(r.get('source') == 'shared_room' for r in records))

    async def test_human_draft_boundaries_late_events_and_undecided_result(self):
        room, server = await self.server(config=MatchConfig(best_of=1, max_games=1))
        red, initial = await self.client(room, server, 'A')
        blue, _ = await self.client(room, server, 'B')
        human, output = self.human(red, initial)
        observation = self.observe(human)
        await until(lambda: human.state.get('state', {}).get('can_act'))
        old = human.state
        epoch = human.epoch
        await human.submit('/analyze 草稿分析')
        await human.submit('/move Rock')
        await human.submit('/endturn')
        b = await self.ready(blue)
        await blue.act(action('Rock'), b['turn_token'], 'draw')
        await until(lambda: human.state['phase'] == 'postmatch')
        self.assertIsNone(human.move)
        self.assertFalse(human.analysis)
        with self.assertRaisesRegex(InvalidResponse, '阶段已变化'):
            await human.submit('/endturn', epoch=epoch)
        revision = human.state['revision']
        before = list(output)
        human.update(old)  # 延迟的旧网络响应不能覆盖阶段或重复显示事件。
        self.assertEqual(human.state['revision'], revision)
        self.assertEqual(output, before)
        self.assertTrue(any('未决' in line for line in output))
        await human.submit('/exit')
        b = await self.ready(blue)
        await blue.act([{'type': 'EXIT'}], b['turn_token'], 'exit')
        self.assertEqual(await asyncio.wait_for(observation, 5), 'complete')
        self.assertEqual(result_exit_code(human.state), 3)

    async def test_closing_server_releases_long_poll_and_pending_input(self):
        room = SharedRoom(log_dir=self.temp.name, output=lambda _: None)
        async with RoomHTTPServer(room, port=0) as server:
            red, _ = await self.client(room, server, 'A')
            blue, _ = await self.client(room, server, 'B')
            await self.ready(red)
            await self.ready(blue)
            state = await red.read()
            waiting = asyncio.create_task(red.wait(state['revision'], 20))
            # 等待 HTTP 请求实际进入房间的长轮询。
            await asyncio.sleep(.05)
        result = await asyncio.wait_for(waiting, 3)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(all(player._future is None for player in room.room.inputs.values()))

    def test_exit_codes_for_cancellation_and_technical_failures(self):
        self.assertEqual(result_exit_code({'status': 'cancelled'}), 130)
        self.assertEqual(result_exit_code({'status': 'failed'}), 1)
        self.assertEqual(result_exit_code({'status': 'failed', 'result': {'status': 'won'}}), 4)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP optional SDK is not installed')
    async def test_dynamic_join_discovery_validation_and_failed_invitation_recovery(self):
        room, server = await self.server()
        connector = self.connector()
        async with Client(create_server(connector)) as client:
            tools = await client.list_tools()
            start = next(t for t in tools.tools if t.name == 'rps_start')
            self.assertTrue({'room_url', 'token', 'name'} <= set(start.input_schema['properties']))
            self.assertFalse(room.clients)
            for tool, params in (('rps_start', {}), ('rps_state', {}),
                                 ('rps_wait', {'after_revision': 0}),
                                 ('rps_act', {'actions': [], 'turn_token': 'old', 'action_id': 'old'})):
                result = (await client.call_tool(tool, params)).structured_content
                self.assertIn('请先调用 rps_start', result['error'])
            invitation = {'room_url': server.url, 'token': room.tokens['A']}
            for params in (
                {'room_url': server.url}, {'token': room.tokens['A']},
                {**invitation, 'room_url': 'https://example.com'},
                {**invitation, 'room_url': 'http://127.0.0.1:bad'},
                {**invitation, 'room_url': 'http://127.0.0.1:0'},
                {**invitation, 'token': 'bad\ntoken'},
                {**invitation, 'name': ''}, {**invitation, 'name': 'bad\nname'},
                {**invitation, 'name': 'x' * 81}, {**invitation, 'token': 'incorrect'},
            ):
                result = (await client.call_tool('rps_start', params)).structured_content
                self.assertIn('error', result)
                self.assertNotIn(room.tokens['A'], repr(result))
                self.assertFalse(room.clients)
            result = (await client.call_tool('rps_start', {**invitation, 'name': 'DSH'})).structured_content
            self.assertEqual(result['state']['seat'], 'Red')
            self.assertEqual(result['state']['player'], 'Red · DSH')
            self.assertNotIn(room.tokens['A'], repr(result))
            player_id = result['state']['player_id']
            again = (await client.call_tool('rps_start', {'player_id': player_id})).structured_content
            self.assertEqual(again['state']['room_id'], room.room_id)
            # 即使当前只有一位玩家，也不能让另一个对话省略身份读取其私有视图。
            for invalid_id in (None, 'unknown-player'):
                for tool, params in (
                    ('rps_state', {}), ('rps_wait', {'after_revision': 0}),
                    ('rps_act', {'actions': [], 'turn_token': 'old', 'action_id': 'old'}),
                ):
                    result = (await client.call_tool(tool, {**params, 'player_id': invalid_id})).structured_content
                    self.assertIn('error', result)
                    self.assertNotIn('state', result)
                    self.assertNotIn(player_id, repr(result))
            omitted = (await client.call_tool('rps_start', {})).structured_content
            self.assertIn('error', omitted)

    async def test_dynamic_players_are_isolated_and_identifiers_cannot_switch_seats(self):
        room, server = await self.server(config=MatchConfig(best_of=1))
        other, other_server = await self.server()
        connector = self.connector()
        invitation = (server.url, room.tokens['A'])
        snapshots = await asyncio.gather(*(connector.start(*invitation, name='Agent') for _ in range(2)))
        red_id = snapshots[0]['player_id']
        red = connector.player_room(red_id)
        client_id = red.client_id
        self.assertEqual([state['room_id'] for state in snapshots], [room.room_id] * 2)
        self.assertEqual([state['player_id'] for state in snapshots], [red_id] * 2)
        self.assertEqual(room.clients, {'A': client_id})
        with self.assertRaisesRegex(InvalidResponse, '玩家名称'):
            await connector.start(*invitation, name='different')
        blue_state = await connector.start(server.url, room.tokens['B'], name='Agent')
        blue_id = blue_state['player_id']
        self.assertNotEqual(red_id, blue_id)
        blue = connector.player_room(blue_id)
        r, b = await asyncio.gather(self.ready(red), self.ready(blue))
        with self.assertRaisesRegex(InvalidResponse, '绑定'):
            await connector.start(server.url, room.tokens['B'], player_id=red_id)
        with self.assertRaises(InvalidResponse):
            await connector.act(action('Rock'), r['turn_token'], 'cross-seat', player_id=blue_id)
        await connector.act(action('Paper'), r['turn_token'], 'move', player_id=red_id)
        await until(lambda: room.room.inputs['A'].view.own_move is not None)
        hidden = await connector.read(player_id=blue_id)
        self.assertEqual(hidden['seat'], 'Blue')
        self.assertNotIn('private-Paper', repr(hidden))
        self.assertNotIn(red_id, repr(hidden))
        self.assertNotIn(room.tokens['A'], repr(hidden))
        replay = await connector.act(action('Paper'), r['turn_token'], 'move', player_id=red_id)
        self.assertTrue(replay['replayed'])
        await asyncio.gather(self.drive(red, 'Paper'), self.drive(blue, 'Rock'))
        new = await connector.start(other_server.url, other.tokens['B'], name='Next')
        self.assertEqual(new['seat'], 'Blue')
        self.assertNotEqual(new['room_id'], room.room_id)
        self.assertNotEqual(new['player_id'], red_id)
        self.assertIsNone(new['turn_token'])
        self.assertEqual((await connector.read(player_id=red_id))['room_id'], room.room_id)
        with self.assertRaises(InvalidResponse):
            await connector.act(action('Paper'), r['turn_token'], 'old', player_id=new['player_id'])

    async def test_dynamic_join_retry_preserves_client_when_join_response_is_lost(self):
        room, server = await self.server()
        connector = self.connector()
        original = RemoteRoom._call
        async def lose_reply(remote, method, **data):
            result = await original(remote, method, **data)
            if method == 'join':
                raise InvalidResponse('模拟响应丢失')
            return result
        with patch.object(RemoteRoom, '_call', lose_reply):
            with self.assertRaisesRegex(InvalidResponse, '响应丢失'):
                await connector.start(server.url, room.tokens['A'])
        remote = next(iter(connector.rooms.values()))
        client_id = remote.client_id
        self.assertEqual(room.clients, {'A': client_id})
        retried = await connector.start(server.url, room.tokens['A'])
        self.assertEqual(retried['seat'], 'Red')
        self.assertEqual(connector.player_room(retried['player_id']).client_id, client_id)
        self.assertIsNotNone(remote.heartbeat)

    async def test_dynamic_join_after_cancellation_or_room_restart(self):
        connector = self.connector()
        stale = self.connector()
        old = SharedRoom(log_dir=self.temp.name, output=lambda _: None)
        async with RoomHTTPServer(old, port=0) as old_server:
            await connector.start(old_server.url, old.tokens['A'])
            await stale.start(old_server.url, old.tokens['B'])
            await old.close()
            next_room, next_server = await self.server()
            state = await connector.start(next_server.url, next_room.tokens['B'])
            self.assertEqual(state['seat'], 'Blue')
            await next_room.close()
            previous = await connector.start(old_server.url, old.tokens['A'])
            self.assertEqual(previous['status'], 'cancelled')
        # 同一地址重建房间后，旧 token 已失效，仍可在同一 MCP 进程内接受新邀请。
        new = SharedRoom(log_dir=self.temp.name, output=lambda _: None)
        async with RoomHTTPServer(new, port=old_server.server.server_port) as new_server:
            state = await stale.start(new_server.url, new.tokens['A'])
            self.assertEqual(state['room_id'], new.room_id)
            state = await connector.start(new_server.url, new.tokens['B'])
            self.assertEqual(state['room_id'], new.room_id)
            self.assertEqual(state['seat'], 'Blue')
        # 服务完全关闭（连接被拒绝）时也能换到另一端口。
        another, another_server = await self.server()
        state = await connector.start(another_server.url, another.tokens['A'])
        self.assertEqual(state['room_id'], another.room_id)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP optional SDK is not installed')
    async def test_independent_stdio_agents_share_room(self):
        # SDK 的 AnyIO 取消作用域必须在创建它的任务内关闭。
        async with AsyncExitStack() as clients_stack:
            await self.stdio_match(clients_stack)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP optional SDK is not installed')
    async def test_two_players_share_one_stdio_connection_and_can_join_new_rooms(self):
        # 模拟同一 DSH 的两个对话：相同 MCP 连接，不同 player_id。
        async with AsyncExitStack() as clients_stack:
            await self.stdio_match(clients_stack, shared=True)

    async def stdio_match(self, clients_stack, shared=False):
        clients = []
        for _ in range(1 if shared else 2):
            params = StdioServerParameters(command=sys.executable, args=[
                '-X', 'utf8', '-m', 'model_rps.mcp_agent.server', '--shared-room'],
                cwd=str(Path(__file__).resolve().parents[1]))
            clients.append(await clients_stack.enter_async_context(Client(params)))
        if shared:
            clients.append(clients[0])
        async def agent(client, room, server, seat, move):
            response = await client.call_tool('rps_start', {
                'room_url': server.url, 'token': room.tokens[seat], 'name': 'same-agent'})
            state = response.structured_content['state']
            player_id = state['player_id']
            self.assertEqual(response.structured_content['rules']['best_of'], room.room.config.best_of)
            self.assertEqual(state['seat'], 'Red' if seat == 'A' else 'Blue')
            serial = 0
            async with asyncio.timeout(20):
                while state['status'] != 'complete':
                    self.assertNotIn(state['status'], ('failed', 'cancelled'))
                    if state['can_submit'] and (state['phase'] == 'postmatch' or state.get('state', {}).get('can_act')):
                        serial += 1
                        commands = [{'type': 'EXIT'}] if state['phase'] == 'postmatch' else action(move)
                        result = await client.call_tool('rps_act', {'actions': commands, 'turn_token': state['turn_token'],
                            'action_id': str(serial), 'after_revision': state['revision'], 'player_id': player_id})
                        self.assertTrue(result.structured_content['accepted'])
                        state = result.structured_content['state']
                    else:
                        result = await client.call_tool('rps_wait', {'after_revision': state['revision'],
                            'timeout_seconds': .1, 'player_id': player_id})
                        state = result.structured_content
                    self.assertEqual(state['player_id'], player_id)
                    self.assertEqual(state['seat'], 'Red' if seat == 'A' else 'Blue')
            return state
        # 两个 stdio 连接只启动一次，先 BO3，再交换席位加入另一房间的 BO1。
        for best_of, seats in ((3, ('A', 'B')), (1, ('B', 'A'))):
            room, server = await self.server(config=MatchConfig(best_of=best_of, initiative='alternating'))
            result = await asyncio.gather(*(agent(client, room, server, seat, 'Paper' if seat == 'A' else 'Rock')
                                           for client, seat in zip(clients, seats)))
            self.assertEqual(result[0]['result']['winner'], 'Red · same-agent')
            self.assertEqual(result[1]['result']['games_played'], (best_of + 1) // 2)
            self.assertTrue(all(state['room_id'] == room.room_id for state in result))
            self.assertNotEqual(result[0]['player_id'], result[1]['player_id'])


if __name__ == '__main__':
    unittest.main()
