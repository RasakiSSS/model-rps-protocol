"""共享房间的人类终端；只持有自己的视图与未提交草稿。"""

import argparse
import asyncio
import os
import sys
from uuid import uuid4

from ..core.commands import CommandKind, parse_command_line
from ..ui.console import Console, display_text
from ..core.game import Event, InvalidResponse
from ..ui.human_terminal import HumanTerminal, UserInterrupted
from .remote import RemoteRoom


class RemoteHumanPlayer:
    def __init__(self, remote, console):
        self.remote, self.console = remote, console
        self.state = {}
        self.epoch = 0
        self.boundary = None
        self.move, self.analysis = None, []
        self.exiting = False
        self.on_update = self.on_boundary = lambda: None
        self.changed = asyncio.Event()
        self.event_cursor = 0

    @property
    def status(self):
        state = self.state
        if state.get('status') == 'not_started':
            return '等待另一位玩家加入'
        if state.get('phase') == 'postmatch':
            return '赛后聊天 · /say 发言，/exit 退出'
        if state.get('status') in ('complete', 'failed', 'cancelled'):
            return '本场已结束'
        view = state.get('state', {})
        permission = '可行动' if view.get('can_act') else '仅聊天'
        return f"第 {state.get('match', {}).get('game', 1)} 局 · Tick {view.get('tick', '?')} · {permission}"

    def update(self, state):
        if state['revision'] >= self.state.get('revision', -1):
            self.state = state
            self.console.names = {key: state['players'][seat] for key, seat in (('A', 'Red'), ('B', 'Blue'))}
            boundary = (state['phase'], state.get('match', {}).get('game'))
            if self.boundary != boundary:
                self.boundary = boundary
                self.epoch += 1
                self.move, self.analysis = None, []
                self.on_boundary()
            event, self.changed = self.changed, asyncio.Event()
            event.set()
            self.on_update()
        for item in state['events']:
            if item['revision'] <= self.event_cursor:
                continue
            self.event_cursor = item['revision']
            self.render(item)

    def render(self, item):
        kind = item['kind']
        if kind == 'game_start':
            self.console.game = item['game']
            self.console.write(f"System：第 {item['game']} 局开始。")
        elif kind == 'game_event':
            data = dict(item['event'])
            data['player'] = next((k for k, v in self.console.names.items() if v == data['player']), None)
            self.console.event(Event(**data))
        elif kind == 'game_end':
            self.console.write(item['text'])
            self.console.write('System：比分 ' + ' : '.join(f'{name} {wins}' for name, wins in item['wins'].items())
                               + f"；平局 {item['draws']}。")
        elif kind == 'match_end':
            self.console.write(item['text'])
        elif kind == 'postmatch_start':
            self.console.write('System：比赛结束，公开双方分析，进入赛后聊天。')
            for game in item['report']['games']:
                for decision in game['decisions']:
                    if decision['summary']:
                        self.console.write(f"System：第 {game['game']} 局 {decision['player']} 分析：{display_text(decision['summary'])}")
        elif kind == 'postmatch_event':
            event = item['event']
            self.console.write(f"{event['player']}：{display_text(event['text'])}" if event['kind'] == 'say'
                               else f"System：{event['player']} 已退出。")

    async def observe(self):
        while self.state['status'] not in ('complete', 'failed', 'cancelled'):
            self.update(await self.remote.wait(self.state['revision']))
        if self.state.get('error'):
            self.console.write('System：' + self.state['error'])
        else:
            self.console.write('System：本场已结束。')
        return self.state['status']

    async def submit(self, line, *, epoch=None):
        if epoch is not None and epoch != self.epoch:
            raise InvalidResponse('阶段已变化，请重新输入。')
        command_text = line.strip().lower()
        if command_text == '/help':
            return '/say 文本：立即聊天；/move Rock|Paper|Scissors、/analyze 文本：暂存；/endturn：提交；/clear；/status；赛后 /exit。Ctrl+C 中止本场。'
        if command_text == '/status':
            view = self.state.get('state', {})
            remaining = view.get('remaining_turns_including_current')
            return (f"{self.status}；自己的拳型：{view.get('own_move') or '未出拳'}；"
                    f"草稿：{self.move.move.value if self.move else '无'}；分析 {len(self.analysis)} 段。"
                    f"本席位分析要求：{self.state['analysis']}。"
                    + (f"剩余行动机会（含本次）：{remaining}。" if remaining is not None else ''))
        if command_text == '/clear':
            self.move, self.analysis = None, []
            return '已清空草稿。'
        command = parse_command_line(line)
        view = self.state.get('state', {})
        if command.kind in (CommandKind.MOVE, CommandKind.ANALYZE, CommandKind.ENDTURN) and not view.get('can_act'):
            raise InvalidResponse('当前没有行动权限，可以聊天。')
        if command.kind == CommandKind.MOVE:
            self.move = command
            return '已暂存出拳，/endturn 时提交。' + ('已有拳型锁定，本次 MOVE 将被忽略。' if view.get('own_move_locked') else '')
        if command.kind == CommandKind.ANALYZE:
            if self.state['analysis'] == 'off':
                raise InvalidResponse('本席位已关闭分析。')
            self.analysis.append(command)
            return '已暂存分析。'
        if command.kind == CommandKind.EXIT and self.state['phase'] != 'postmatch':
            raise InvalidResponse('/exit 只用于赛后退出；Ctrl+C 中止本场。')
        boundary, tick = self.epoch, view.get('tick')
        while not self.state['can_submit']:
            if self.state['status'] in ('not_started', 'complete', 'failed', 'cancelled'):
                raise InvalidResponse('尚未开赛或本场已结束。')
            await self.changed.wait()
        if boundary != self.epoch or (command.kind == CommandKind.ENDTURN and tick != self.state.get('state', {}).get('tick')):
            raise InvalidResponse('行动机会已变化，请重新确认。')
        commands = [command]
        if command.kind == CommandKind.ENDTURN:
            commands = [*self.analysis, *([self.move] if self.move else []), command]
        # 每条确认输入只有一个提交 ID；网络失败不会自动换 ID 重复执行。
        reply = await self.remote.act([c.to_dict() for c in commands], self.state['turn_token'], uuid4().hex,
                                      self.state['revision'])
        self.update(reply['state'])
        if command.kind == CommandKind.ENDTURN:
            self.move, self.analysis = None, []
        if command.kind == CommandKind.EXIT:
            self.exiting = True


def result_exit_code(state):
    if state['status'] == 'cancelled':
        return 130
    result = state.get('result', {})
    if state['status'] == 'complete':
        return 3 if result.get('status') == 'undecided' else 0
    return 4 if result.get('status') in ('won', 'undecided') else 1


async def play(remote):
    try:
        initial = await remote.start()
        names = {key: initial['players'][seat] for key, seat in (('A', 'Red'), ('B', 'Blue'))}
        console = Console(names)
        config = remote.config
        initiative = '同时行动' if config.initiative == 'simultaneous' else '轮流行动'
        moves = '首次出拳锁定' if config.move_policy == 'locked' else '允许改拳'
        console.write(f"System：BO{config.best_of}，先赢 {config.target_wins} 局获胜，最多 {config.max_games} 局；"
                      f"{initiative}；{moves}。")
        console.private(f"本席位策略分析要求：{initial['analysis']}；/help 查看指令。")
        player = RemoteHumanPlayer(remote, console)
        player.update(initial)
        identity = 'A' if initial['seat'] == 'Red' else 'B'
        await HumanTerminal(player, identity, console).run(player.observe())
        return result_exit_code(player.state)
    finally:
        await remote.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='人类从独立终端加入本机共享房间。')
    parser.add_argument('--room-url', required=True)
    parser.add_argument('--token', default=os.environ.get('RPS_SEAT_TOKEN'))
    parser.add_argument('--name', default='玩家')
    args = parser.parse_args(argv)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('请在交互终端运行玩家客户端。')
        return 2
    try:
        return asyncio.run(play(RemoteRoom(args.room_url, args.token, name=args.name)))
    except (KeyboardInterrupt, UserInterrupted):
        return 130
    except (ValueError, InvalidResponse) as exc:
        print(str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
