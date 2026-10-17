"""比赛与赛后聊天的测试输入；仅用于构建有效协议样例。"""

from copy import deepcopy
import json


def turn_payload(actions, analysis="策略分析"):
    commands = [{"type": "ANALYZE", "text": analysis}]
    commands.extend(actions)
    commands.append({"type": "ENDTURN"})
    return {"actions": commands}


def postmatch_payload(text=None, *, exit=True):
    actions = [] if text is None else [{"type": "SAY", "text": text}]
    if exit:
        actions.append({"type": "EXIT"})
    return {"actions": actions}


def complete_public_report(messages):
    """从请求中的全部增量记录还原公开材料，检测重复序号内容冲突。"""
    events, report = {}, None

    def collect(number, rows):
        game = events.setdefault(number, {})
        for row in rows:
            event = {key: value for key, value in row.items() if key != 'sequence'}
            sequence = row['sequence']
            if sequence in game and game[sequence] != event:
                raise AssertionError('同一事件序号对应不同内容')
            game[sequence] = event

    for message in messages:
        if message['role'] != 'user':
            continue
        context = json.loads(message['content'])
        match = context.get('match', {})
        if 'state' in context:
            collect(match.get('game', 1), context['state']['history'])
            if previous := match.get('last_game'):
                collect(previous['game'], previous['events'])
        elif 'games' in match:
            report = deepcopy(match)
            for game in report['games']:
                collect(game['game'], game['events'])
    if report is None:
        raise AssertionError('没有赛后公开材料')
    for game in report['games']:
        rows = events[game['game']]
        if sorted(rows) != list(range(1, game['event_cursor'] + 1)):
            raise AssertionError('公开事件缺失')
        game['events'] = [rows[index] for index in sorted(rows)]
        game.pop('history_after')
        game.pop('event_cursor')
    return report
