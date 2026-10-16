"""比赛与复盘的测试输入；仅用于构建有效协议样例。"""


def turn_payload(actions, analysis="策略分析"):
    commands = [{"type": "ANALYZE", "text": analysis}]
    commands.extend(actions)
    commands.append({"type": "ENDTURN"})
    return {"actions": commands}


def review_payload(text):
    return {"actions": [{"type": "REVIEW", "text": text}]}
