"""可取消的异步终端输入；玩家提示只在本地显示，不写入公共聊天。"""

import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session, run_in_terminal
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import create_output

from .game import InvalidResponse


class UserInterrupted(Exception):
    """Ctrl+C 或关闭输入；由 CLI 统一按用户中止处理。"""


async def wait_for_resume(console, session=None):
    """双方均无请求时由终端操作者恢复交流，不代替玩家发言或 EXIT。"""
    console.write("System：赛后暂无新消息，进入待机；仍在场的玩家尚未退出。")
    if session is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.output("当前无交互终端，保持待机；可中止进程，已有日志保留。")
        await asyncio.Event().wait()
    session = session or PromptSession(output=create_output(), erase_when_done=True)
    while True:
        try:
            line = await session.prompt_async("赛后控制 > ", key_bindings=KeyBindings(),
                bottom_toolbar="/resume 邀请仍在场的玩家继续交流 · Ctrl+C 中止")
        except (EOFError, KeyboardInterrupt):
            raise UserInterrupted from None
        if line.strip().lower() == "/resume":
            console.write("System：已邀请仍在场的玩家继续交流。")
            return
        console.private("输入 /resume 恢复交流；Ctrl+C 中止。此处不能代替玩家发言或退出。")


class HumanTerminal:
    def __init__(self, player, identity, console, *, session=None):
        self.player, self.identity, self.console = player, identity, console
        self.accepted_epoch = player.epoch
        bindings = KeyBindings()

        @bindings.add("enter")
        def accept(event):
            self.accepted_epoch = player.epoch
            event.current_buffer.validate_and_handle()

        self.bindings = bindings
        # 显式使用平台输出，避免 Windows 的 TERM=dumb 触发另一套简化应用，
        # 覆盖 Enter 绑定并使输入保护和状态刷新指向错误的应用实例。
        self.session = session or PromptSession(output=create_output(), erase_when_done=True)

    def _update(self):
        self.session.app.invalidate()

    def _boundary(self):
        # 未发送的聊天文字可继续编辑；旧局的行动输入不能误提交到下一局。
        buffer = self.session.default_buffer
        text = buffer.text.lstrip().lower()
        if any(text.startswith(command) for command in ("/move", "/analyze", "/endturn")):
            buffer.reset()
        self._update()

    def _prompt(self):
        style = ("ansibrightred" if self.identity == "A" else "ansibrightblue") if self.console.color else ""
        return [(style, self.console.names[self.identity]), ("", " > ")]

    async def _read(self):
        while not self.player.exiting:
            try:
                line = await self.session.prompt_async(self._prompt, key_bindings=self.bindings,
                                                       bottom_toolbar=lambda: self.player.status)
            except (EOFError, KeyboardInterrupt):
                raise UserInterrupted from None
            if not line.strip():
                continue
            try:
                message = await self.player.submit(line, epoch=self.accepted_epoch)
            except InvalidResponse as exc:
                message = str(exc)
            if message:
                self.console.private(message)

    async def wait_for_resume(self):
        await wait_for_resume(self.console, self.session)

    async def run(self, match):
        self.player.on_update = self._update
        self.player.on_boundary = self._boundary
        original_output = self.console.output
        lines = asyncio.Queue()
        self.console.output = lines.put_nowait

        async def display():
            # 在同一事件循环和 AppSession 内打印并重绘输入，避免后台打印线程
            # 丢失会话上下文，将对手发言直接写到正在编辑的输入后面。
            while (line := await lines.get()) is not None:
                await run_in_terminal(lambda: original_output(line))

        tasks = []
        try:
            with create_app_session(input=self.session.app.input, output=self.session.app.output):
                game = asyncio.create_task(match)
                reader = asyncio.create_task(self._read())
                printer = asyncio.create_task(display())
                tasks.extend((game, reader, printer))
                try:
                    self.console.private("使用 /help 查看指令；输入期间仍可收到对手发言。")
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    if printer in done:
                        printer.result()
                    if reader in done:
                        reader.result()  # 正常 EXIT 只结束输入；异常则取消整场运行。
                    if game not in done:
                        await asyncio.wait((game, printer), return_when=asyncio.FIRST_COMPLETED)
                        if printer.done():
                            printer.result()
                    return await game
                finally:
                    game.cancel()
                    reader.cancel()
                    await asyncio.gather(game, reader, return_exceptions=True)
                    lines.put_nowait(None)
                    await printer
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.player.on_update = self.player.on_boundary = lambda: None
            self.console.output = original_output
