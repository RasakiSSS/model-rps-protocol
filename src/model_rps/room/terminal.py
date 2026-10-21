"""服务端本地管理终端；管理指令不暴露给玩家 HTTP / MCP 接口。"""

import asyncio
import json
import sys
import threading

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.output import create_output
from prompt_toolkit.patch_stdout import patch_stdout

from ..ui.console import display_text
from ..core.game import InvalidResponse


HELP = '/start 新开一场 · /stop 停止本场 · /status 状态 · /quit 关闭服务 · /help 帮助'


class RoomTerminal:
    def __init__(self, server, *, session=None, output=None):
        self.server, self.service = server, server.service
        self.session = session
        self.output = output or (lambda text: print(text, flush=True))
        self.service.current.room.output = self.output

    def invitations(self):
        self.output(f'第 {self.service.number} 场已开放加入：{self.server.url}；双方到齐后开赛。')
        for seat, token in self.service.tokens.items():
            label = 'Red' if seat == 'A' else 'Blue'
            self.output(f'{label} 连接命令（在另一个终端执行）：')
            self.output(f'.\\.venv\\Scripts\\python.exe -X utf8 -m model_rps.room.client --room-url {self.server.url} --token {token} --name {label}')
            invitation = json.dumps({'room_url': self.server.url, 'token': token}, ensure_ascii=False)
            self.output(f'{label} Agent 邀请（仅发送给这一方，让它调用 rps_start）：{invitation}')

    def status(self):
        current = self.service.current
        state = current.room.status
        phases = {'not_started': '等待双方加入', 'running': '比赛中', 'postmatch': '赛后聊天',
                  'complete': '已完成', 'failed': '技术异常', 'cancelled': '已中止'}
        lines = [f'第 {self.service.number} 场 · {"待机 / " if self.service.idle else ""}{phases[state]} · BO{current.room.config.best_of}']
        for seat, label in (('A', 'Red'), ('B', 'Blue')):
            status = '已结束' if self.service.idle else ('已退出' if current.room.inputs[seat].exiting else
                       '已加入' if seat in current.clients else '等待加入')
            lines.append(f'{label}：{display_text(current.room.names[seat])}（{status}）')
        result = current.room.result
        # 未结场期间最近一局的比分也可查询。
        score = result or next((e for e in reversed(current.room.events) if e['kind'] == 'game_end'), None)
        if score:
            lines.append(f"比分 Red {score['wins']['A']} : {score['wins']['B']} Blue；平局 {score['draws']}。")
        if current.room.logs:
            lines.append(f'日志：{current.room.logs.path}')
        return '\n'.join(lines)

    async def execute(self, line):
        command = line.strip().lower()
        try:
            if command == '/start':
                await self.service.start()
                self.invitations()
            elif command == '/stop':
                await self.service.stop()
                self.output('服务端待机；输入 /start 新开一场，/quit 关闭服务。')
            elif command == '/status':
                self.output(self.status())
            elif command == '/quit':
                return False
            elif command == '/help':
                self.output(HELP)
            elif command:
                self.output('未知管理指令。' + HELP)
        except InvalidResponse as exc:
            self.output(str(exc))
        return True

    async def watch(self):
        announced = None
        while True:
            current = self.service.current
            if self.service.idle and announced != current.room_id:
                self.output('本场已结束，服务端待机；输入 /start 新开一场，/quit 关闭服务。')
                announced = current.room_id
            if self.session:
                self.session.app.invalidate()
            events = [asyncio.create_task(event.wait()) for event in (current.room.changed, self.service.changed)]
            try:
                await asyncio.wait(events, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in events:
                    task.cancel()
                await asyncio.gather(*events, return_exceptions=True)

    async def _piped(self):
        # 管道命令可用于脚本；EOF 保持服务运行。守护线程不阻塞 Ctrl+C 清理。
        loop, lines = asyncio.get_running_loop(), asyncio.Queue()
        def read():
            while True:
                line = sys.stdin.readline()
                try:
                    loop.call_soon_threadsafe(lines.put_nowait, line or None)
                except RuntimeError:
                    return
                if not line:
                    return
        threading.Thread(target=read, daemon=True).start()
        while (line := await lines.get()) is not None:
            if not await self.execute(line):
                return
        await asyncio.Event().wait()

    async def run(self):
        if self.session is None and sys.stdin.isatty() and sys.stdout.isatty():
            self.session = PromptSession(output=create_output(), erase_when_done=True)
        self.output('房间服务已启动；本进程内沿用双方 token。' + HELP)
        self.invitations()
        watcher = asyncio.create_task(self.watch())
        try:
            if self.session is None:
                await self._piped()
            else:
                with create_app_session(input=self.session.app.input, output=self.session.app.output), patch_stdout():
                    while True:
                        try:
                            line = await self.session.prompt_async('服务端 > ', bottom_toolbar=HELP)
                        except EOFError:
                            return
                        if not await self.execute(line):
                            return
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
