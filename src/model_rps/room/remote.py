"""人类终端与 MCP 共用的房间连接；不持有另一席位的令牌或私有视图。"""

import asyncio
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ..config import text_value
from ..core.game import InvalidResponse
from ..engine.match import MatchConfig


class RemoteRoom:
    def __init__(self, url, token, *, name='玩家', kind='human'):
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError):
            raise ValueError('房间地址须为本机 http://127.0.0.1:端口。') from None
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/') or port == 0):
            raise ValueError('房间地址须为本机 http://127.0.0.1:端口。')
        if not isinstance(token, str) or not token.strip() or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError('请填写有效的席位连接凭证。')
        text_value(name, '玩家名称')
        if len(name) > 80:
            raise ValueError('玩家名称最多 80 字符。')
        self.url, self.token, self.name, self.kind = url.rstrip('/'), token, name, kind
        self.client_id = uuid4().hex
        self.client = None
        self.config = MatchConfig()
        self.joined = False
        self.heartbeat = None
        self.connection_error = None
        self.room_id = None

    async def _call(self, method, **data):
        if self.client is None:
            raise InvalidResponse('请先加入房间。')
        try:
            envelope = {'client_id': self.client_id}
            if method != 'info':
                envelope['room_id'] = self.room_id
            response = await self.client.post('/' + method, json={**envelope, **data})
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise InvalidResponse('无法连接房间；连接可能已断开。') from None
        if response.status_code != 200:
            raise InvalidResponse(body.get('error', '房间请求失败。'))
        return body

    async def prepare(self):
        """只发现当前场次，不占席；绑定后不能自动跳到另一场。"""
        if self.client is None:
            self.client = httpx.AsyncClient(base_url=self.url, headers={'Authorization': 'Bearer ' + self.token},
                                             timeout=30, trust_env=False, follow_redirects=False)
        if self.room_id is None:
            info = await self._call('info')
            self.room_id = info['room_id']

    async def start(self):
        await self.prepare()
        result = await self._call('join', name=self.name, kind=self.kind)
        self.config = MatchConfig(**result['config'])
        self.joined = True
        if self.heartbeat is None:
            self.heartbeat = asyncio.create_task(self._keep_alive())
        return result['state']

    async def _keep_alive(self):
        try:
            while True:
                await asyncio.sleep(10)
                await self._call('ping')
        except InvalidResponse as exc:
            self.connection_error = str(exc)

    async def read(self, after=0):
        return await self._call('state', after_revision=after)

    async def wait(self, after, timeout=20):
        return await self._call('wait', after_revision=after, timeout_seconds=timeout)

    async def act(self, actions, turn_token, action_id, after_revision=0):
        return await self._call('act', actions=actions, turn_token=turn_token, action_id=action_id,
                                after_revision=after_revision)

    async def close(self):
        if self.heartbeat:
            self.heartbeat.cancel()
            await asyncio.gather(self.heartbeat, return_exceptions=True)
            self.heartbeat = None
        if self.client:
            if self.joined:
                try:
                    await asyncio.wait_for(self._call('leave'), 2)
                except (InvalidResponse, TimeoutError):
                    pass
            await self.client.aclose()
            self.client = None
        self.joined = False


class RoomConnector:
    """一个 MCP 进程可承载多个玩家；player_id 固定绑定场次和席位。"""

    def __init__(self, *, name='Agent'):
        text_value(name, '玩家名称')
        if len(name) > 80:
            raise ValueError('玩家名称最多 80 字符。')
        self.default_name = name
        self.rooms = {}
        self._invitations = {}
        self._join_lock = asyncio.Lock()

    def player_room(self, player_id):
        if not isinstance(player_id, str) or player_id not in self.rooms:
            raise InvalidResponse('请先调用 rps_start，之后每次调用都传入它返回的 player_id；不能省略或猜测。')
        return self.rooms[player_id]

    async def start(self, room_url=None, token=None, name=None, player_id=None):
        async with self._join_lock:
            if player_id is not None:
                remote = self.player_room(player_id)
                if any(value is not None for value in (room_url, token, name)):
                    raise InvalidResponse('player_id 已绑定原房间和席位；刷新时只传 player_id，新邀请请省略它。')
                return {**await remote.start(), 'player_id': player_id}
            if room_url is None or token is None:
                raise InvalidResponse('请先调用 rps_start，提供 room_url 和 token；name 可选。已有连接可传 player_id。')
            try:
                candidate = RemoteRoom(room_url, token,
                                       name=self.default_name if name is None else name, kind='mcp')
            except ValueError as exc:
                raise InvalidResponse(str(exc)) from None
            try:
                await candidate.prepare()
            except BaseException:
                await candidate.close()
                raise
            invitation = (candidate.url, candidate.token, candidate.room_id)
            player_id = self._invitations.get(invitation)
            if player_id is None:
                player_id = uuid4().hex
                self.rooms[player_id] = candidate
                self._invitations[invitation] = player_id
            else:
                await candidate.close()
            remote = self.player_room(player_id)
            if name is not None and name != remote.name:
                raise InvalidResponse('重复加入不能修改玩家名称；请使用原名称或省略 name。')
            # 包括 join 响应丢失后的重试；复用 client_id，不重复占席。
            return {**await remote.start(), 'player_id': player_id}

    async def read(self, after=0, *, player_id=None):
        return {**await self.player_room(player_id).read(after), 'player_id': player_id}

    async def wait(self, after, timeout=20, *, player_id=None):
        return {**await self.player_room(player_id).wait(after, timeout), 'player_id': player_id}

    async def act(self, actions, turn_token, action_id, after_revision=0, *, player_id=None):
        result = await self.player_room(player_id).act(actions, turn_token, action_id, after_revision)
        return {**result, 'state': {**result['state'], 'player_id': player_id}}

    async def close(self):
        async with self._join_lock:
            await asyncio.gather(*(room.close() for room in self.rooms.values()))
