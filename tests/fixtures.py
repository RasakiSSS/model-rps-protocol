"""比赛与赛后聊天的测试输入；仅用于构建有效协议样例。"""


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
