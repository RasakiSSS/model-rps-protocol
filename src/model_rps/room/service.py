"""常驻房间服务：固定席位凭证，每场 BO 使用独立状态和连接。"""

import asyncio

from ..core.game import InvalidResponse
from .shared import SharedRoom


TERMINAL_STATES = ('complete', 'failed', 'cancelled')


class RoomService:
    def __init__(self, room):
        self.current = room
        self.tokens = dict(room.tokens)
        self.number = 1
        self.changed = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def idle(self):
        return self.current.room.status in TERMINAL_STATES

    def identity(self, token):
        return self.current.identity(token)

    async def start(self):
        async with self._lock:
            if not self.idle:
                raise InvalidResponse('当前场仍在等待加入、比赛或赛后聊天；请先 /stop，或等待双方 /exit。')
            old = self.current
            await old.close()
            self.current = SharedRoom(config=old.room.config,
                analysis=tuple(old.room.inputs[seat].analysis_policy for seat in ('A', 'B')),
                log_dir=old.room.log_dir, output=old.room.output,
                disconnect_timeout=old.disconnect_timeout, tokens=self.tokens)
            self.number += 1
            changed, self.changed = self.changed, asyncio.Event()
            changed.set()
            return self.current

    async def stop(self):
        async with self._lock:
            if not self.idle:
                self.current.room.error = '服务端已停止本场，不计作弃权。'
                await self.current.close()

    async def dispatch(self, identity, method, data):
        # 在第一次 await 前捕获场次；正在等待的旧请求仍只返回旧场结果。
        current = self.current
        if not isinstance(data, dict):
            raise InvalidResponse('请求必须是 JSON 对象。')
        data = dict(data)
        room_id = data.pop('room_id', None)
        if method == 'info':
            if set(data) != {'client_id'} or room_id is not None:
                raise InvalidResponse('房间信息请求包含无效字段。')
            return {'room_id': current.room_id, 'status': current.room.status}
        if room_id != current.room_id:
            raise InvalidResponse('场次已更换或缺少场次标识；请使用原邀请重新加入，MCP 重新调用 rps_start 并省略旧 player_id。')
        return await current.dispatch(identity, method, data)

    async def monitor(self):
        # 每次换场取消旧监视器；不能让旧心跳影响新比赛。
        while True:
            current, changed = self.current, self.changed
            monitor = asyncio.create_task(current.monitor())
            next_match = asyncio.create_task(changed.wait())
            try:
                done, _ = await asyncio.wait((monitor, next_match), return_when=asyncio.FIRST_COMPLETED)
                if monitor in done:
                    monitor.result()
            finally:
                monitor.cancel()
                next_match.cancel()
                await asyncio.gather(monitor, next_match, return_exceptions=True)

    async def close(self):
        await self.current.close()
