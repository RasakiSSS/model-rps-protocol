"""两个外部输入端共享同一场 BO；连接凭证固定绑定席位。"""

import asyncio
from contextlib import asynccontextmanager
import secrets
from time import monotonic
from uuid import uuid4

from ..config import text_value
from ..players.external import ExternalPlayer
from ..core.game import InvalidResponse
from .base import Room


class SharedRoom:
    def __init__(self, *, config=None, analysis=('optional', 'optional'), log_dir='logs', output=None,
                 disconnect_timeout=90, tokens=None):
        players = {identity: ExternalPlayer(analysis=policy) for identity, policy in zip(('A', 'B'), analysis)}

        @asynccontextmanager
        async def peer():
            yield players['B']

        self.tokens = dict(tokens) if tokens is not None else {identity: secrets.token_urlsafe(24) for identity in players}
        self.room = Room(peer, config=config, external_players=players, log_dir=log_dir,
                         secrets=tuple(self.tokens.values()), output=output, source="shared_room")
        self.room.names = {'A': 'Red · 等待加入', 'B': 'Blue · 等待加入'}
        self.room_id = uuid4().hex
        self.clients = {}
        self.seen = {}
        self.disconnect_timeout = disconnect_timeout

    def identity(self, token):
        if not isinstance(token, str) or not token.isascii():
            return None
        return next((seat for seat, value in self.tokens.items() if secrets.compare_digest(token, value)), None)

    def state(self, identity, after=0):
        return {**self.room.snapshot(after, identity=identity), 'room_id': self.room_id,
                'players': {'Red': self.room.names['A'], 'Blue': self.room.names['B']},
                'joined': {'Red': 'A' in self.clients, 'Blue': 'B' in self.clients}}

    async def dispatch(self, identity, method, data):
        if not isinstance(data, dict):
            raise InvalidResponse('请求必须是 JSON 对象。')
        data = dict(data)
        client = data.pop('client_id', None)
        if not isinstance(client, str) or not 1 <= len(client) <= 100:
            raise InvalidResponse('缺少有效的客户端标识。')
        if method == 'join':
            if set(data) != {'name', 'kind'} or data['kind'] not in ('human', 'mcp'):
                raise InvalidResponse('加入请求需要 name 和 human/mcp 类型。')
            text_value(data['name'], '玩家名称')
            if len(data['name']) > 80:
                raise InvalidResponse('玩家名称最多 80 字符。')
            if identity in self.clients and self.clients[identity] != client:
                raise InvalidResponse('这个席位已有客户端连接。')
            if identity not in self.clients:
                if self.room.status != 'not_started':
                    raise InvalidResponse('本房间已经结束，不能重新加入。')
                self.clients[identity] = client
                self.room.names[identity] = f"{'Red' if identity == 'A' else 'Blue'} · {data['name']}"
                self.room.inputs[identity].input_source = data['kind']
                self.room._changed()
            self.seen[identity] = monotonic()
            if len(self.clients) == 2:
                await self.room.start()
            config = self.room.config
            return {'config': {'best_of': config.best_of, 'max_games': config.max_games,
                              'initiative': config.initiative.value, 'move_policy': config.move_policy.value},
                    'state': self.state(identity)}
        if self.clients.get(identity) != client:
            raise InvalidResponse('请先使用本客户端加入房间。')
        self.seen[identity] = monotonic()
        if method in ('ping', 'leave'):
            if data:
                raise InvalidResponse('请求包含多余字段。')
            if method == 'leave' and self.room.status not in ('complete', 'failed', 'cancelled'):
                if not self.room.inputs[identity].exiting:
                    await self.abort(identity)
            return {'ok': True}
        if method == 'state' and set(data) <= {'after_revision'}:
            return self.state(identity, data.get('after_revision', 0))
        if method == 'wait' and set(data) <= {'after_revision', 'timeout_seconds'}:
            after = data.get('after_revision', 0)
            await self.room.wait(after, data.get('timeout_seconds', 20), identity=identity)
            return self.state(identity, after)
        if method == 'act' and set(data) in ({'actions', 'turn_token', 'action_id'},
                                            {'actions', 'turn_token', 'action_id', 'after_revision'}):
            reply = await self.room.act(**data, identity=identity)
            reply['state'] = self.state(identity, data.get('after_revision', 0))
            return reply
        raise InvalidResponse('未知请求或字段不正确。')

    async def abort(self, identity):
        self.room.error = f"{'Red' if identity == 'A' else 'Blue'} 连接已断开，本场中止，不计作弃权。"
        await self.room.close()

    async def monitor(self):
        while True:
            await asyncio.sleep(min(5, self.disconnect_timeout / 2))
            if self.room.status in ('complete', 'failed', 'cancelled'):
                continue
            for seat, last in tuple(self.seen.items()):
                if not self.room.inputs[seat].exiting and monotonic() - last > self.disconnect_timeout:
                    await self.abort(seat)
                    break

    async def close(self):
        await self.room.close()
