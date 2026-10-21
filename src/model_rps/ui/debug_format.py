"""调试记录的只读展示；策略分析可见，接口思考与完整请求留在日志。"""

import json
import unicodedata


REASONS = {
    "game_start": "开局", "turn_changed": "行动轮换", "tick_started": "新 Tick",
    "action_available": "行动机会", "action_ended": "自己的行动已结束", "opponent_say": "对手发言",
    "postmatch_start": "赛后邀请", "opponent_event": "对手发言或退出", "handoff": "玩家交接",
    "operator_resume": "终端恢复交流", "continue_after_exit": "对手离开后继续发言",
}
GESTURES = {"Rock": "石头", "Paper": "布", "Scissors": "剪刀"}


def inline(value):
    text = "未提供" if value is None else str(value)
    return "".join(f"\\u{ord(char):04x}" if unicodedata.category(char).startswith("C") else char for char in text)


def block(label, value, *, limit=None):
    text = "未提供" if value is None else str(value)
    omitted = limit is not None and len(text) > limit
    if omitted:
        text = text[:limit]
    lines = [f"  {label}：", *("    " + inline(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))]
    if omitted:
        lines.append("    ……窗口仅展示前段，完整内容见 debug.log。")
    return "\n".join(lines)


def gesture(value):
    return GESTURES.get(value, inline(value)) if value is not None else "未出拳"


def actions_detail(actions):
    lines = []
    for action in actions:
        kind = action.get("type")
        if kind == "SAY":
            lines.append(block("SAY", action.get("text")))
        elif kind == "MOVE":
            lines.append(f"  MOVE：{gesture(action.get('move'))}")
        elif kind in ("WAIT", "ENDTURN"):
            lines.append("  ENDTURN：结束行动，保留已有拳型")
        else:
            lines.append("  " + inline(json.dumps(action, ensure_ascii=False)))
    return lines


def error_detail(body):
    """只提取服务错误，避免把错误响应附带的完整请求或思考字段刷到窗口。"""
    if not body:
        return "  服务未提供错误正文。"
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError, RecursionError):
        return block("服务错误正文", body, limit=4000)
    if not isinstance(parsed, dict):
        return "  服务错误结构未识别，完整内容见 debug.log。"
    error = parsed.get("error", parsed)
    if isinstance(error, str):
        return block("服务错误", error, limit=4000)
    if isinstance(error, dict):
        fields = {key: error[key] for key in ("message", "type", "code", "param", "status")
                  if key in error and isinstance(error[key], (str, int, float, bool, type(None)))}
        if fields:
            return block("服务错误", json.dumps(fields, ensure_ascii=False, indent=2), limit=4000)
    return "  服务错误结构未识别，完整内容见 debug.log。"


def request_detail(row):
    context = row.get("state", row.get("context", {}))
    detail = "人类输入已提交，开始校验" if row.get("source") == "human" else "等待响应"
    reasons = context.get("request_reasons", [])
    if reasons:
        detail += "；触发：" + "、".join(inline(REASONS.get(reason, reason)) for reason in reasons)
    lines = [detail]
    if "can_act" in context:
        permission = "可行动或聊天" if context["can_act"] else "仅聊天"
        locked = "已锁定" if context.get("own_move_locked") else "未锁定"
        peer = "已公开提交" if context.get("opponent_committed") else "未公开提交"
        lines.append(f"  权限：{permission}；自己的拳型：{gesture(context.get('own_move'))}（{locked}）；对手：{peer}")
        lines.append(f"  本方剩余机会：{inline(context.get('remaining_turns_including_current'))}；"
                     f"公开事件总数：{inline(context.get('event_cursor'))}；消息通知：{len(context.get('notifications', []))}")
        if context.get("settlement_tick") is not None:
            lines.append(f"  结算 Tick：{inline(context['settlement_tick'])}")
        notified = set(context.get("notifications", []))
        for event in context.get("history", []):
            if event.get("sequence") in notified:
                lines.append(block(f"新消息 #{inline(event.get('sequence'))} · {inline(event.get('actor') or event.get('player'))}",
                                   event.get("text", "")))
    elif row.get("phase") == "postmatch":
        exited = "、".join(inline(player) for player, left in context.get("exited", {}).items() if left) or "无"
        lines.append(f"  权限：赛后聊天 / EXIT；新事件：{len(context.get('events', []))}；已退出：{exited}")
    if row.get("correction"):
        lines.append(block("纠错反馈", row["correction"]))
    return "\n".join(lines)


def format_record(row: dict) -> str | None:
    """输入为已脱敏的日志记录；所有外部文字转义，长文本保留缩进与换行。"""
    kind = row.get("kind")
    # 不输出原生思考片段、原始成功响应或完整历史请求；正文经校验后按指令展示。
    if kind in {"stream_delta", "raw_response", "http_request"}:
        return None
    author = inline(row.get("actor") or row.get("player", "System"))
    label = author
    if "attempt" in row or "request_id" in row or kind == "handoff":
        phase = "赛后聊天" if row.get("phase") == "postmatch" else f"第 {inline(row.get('game'))} 局 Tick {inline(row.get('tick'))}"
        if row.get("phase") == "chat":
            phase += " · 聊天"
        label += f" · {phase}"
        if row.get("request_id") is not None:
            label += f" · 响应 #{inline(row['request_id'])}"
        if "attempt" in row:
            label += f" · 第 {inline(row['attempt'])} 次请求"
    if kind == "tick":
        detail = inline(row.get("text", "")).replace("\\u000a", " ").strip()
    elif kind == "request_start":
        detail = request_detail(row)
    elif kind == "request_config":
        detail = (f"请求设置：模型 {inline(row.get('model'))}；协议 {inline(row.get('protocol'))}；"
                  f"思考等级 {inline(row.get('requested_thinking'))}；流式 {'开启' if row.get('stream') else '关闭'}；"
                  f"输出上限 {inline(row.get('max_output_tokens'))} token")
        if row.get("stream"):
            detail += f"\n  流式时限：首包 {inline(row.get('stream_first_timeout'))} 秒；空闲 {inline(row.get('stream_idle_timeout'))} 秒"
    elif kind == "decision":
        lines = ["行动校验通过（尚待调度）", block("策略分析（ANALYZE）", row.get("summary") or "本次未提交分析。")]
        lines += actions_detail(row.get("actions", []))
        for action in row.get("ignored_actions", []):
            lines.append(f"  MOVE：{gesture(action.get('move'))}；已锁定，调度执行时将忽略")
        if any(action.get("type") == "MOVE" for action in row.get("actions", [])) or row.get("ignored_actions"):
            lines.append("  ENDTURN：结束本次行动")
        detail = "\n".join(lines)
    elif kind == "chat_validated":
        detail = "聊天校验通过（尚待公开）"
        if row.get("messages"):
            detail += "\n" + "\n".join(block("SAY", message) for message in row["messages"])
        elif row.get("message_count", 0):
            detail += f"；{inline(row['message_count'])} 条发言（旧日志未记录正文）"
        else:
            detail += "；保持沉默"
    elif kind == "postmatch_response":
        lines = ["赛后响应校验通过（尚待公开）"]
        lines += [block("SAY", message) for message in row.get("messages", [])]
        if row.get("exit"):
            lines.append("  EXIT：退出赛后聊天")
        elif row.get("silent"):
            lines.append("  保持沉默，仍在场")
        detail = "\n".join(lines)
    elif kind == "validation_error":
        detail = "响应校验失败，本次指令未执行\n" + block("原因", row.get("error"))
        if row.get("content") is not None:
            detail += "\n" + block("无效响应正文", row["content"], limit=4000)
    elif kind == "stream_status":
        detail = {"waiting": "等待响应", "thinking": "接收思考（文本仅写日志）", "generating": "接收正文"}.get(row.get("status"), "接收中")
    elif kind == "request_end":
        status = {"valid": "校验通过", "rejected": "失败", "cancelled": "已取消"}.get(row.get("status"), row.get("status"))
        elapsed = row.get("elapsed_seconds")
        duration = f"{elapsed:.2f} 秒" if isinstance(elapsed, (int, float)) else "未记录"
        detail = f"请求结束：{inline(status)}；耗时 {duration}"
        if row.get("error"):
            detail += "\n" + block("错误", row["error"])
        if row.get("correction"):
            detail += "\n" + block("纠错反馈", row["correction"])
        if "will_retry" in row:
            detail += "\n  将重试" if row["will_retry"] else "\n  不再重试"
    elif kind == "http_response":
        detail = f"HTTP {inline(row.get('status'))}"
        if isinstance(row.get("status"), int) and row["status"] >= 400 and row.get("body"):
            detail += "\n" + error_detail(row["body"])
    elif kind == "http_error_body":
        detail = "服务拒绝请求\n" + error_detail(row.get("body"))
    elif kind == "stream_end":
        detail = (f"流式接收结束：{inline(row.get('status'))}；分片 {inline(row.get('chunks'))}；"
                  f"结束原因 {inline(row.get('finish_reason'))}（是否执行以调度结果为准）")
    elif kind == "reasoning":
        detail = f"接口思考：{inline(row.get('status'))}；token {inline(row.get('reasoning_tokens'))}；文本仅写日志"
    elif kind == "context_usage":
        current, delta = row.get("input_tokens"), row.get("input_tokens_delta")
        detail = (f"输入 token：{inline(current)}；较上次：{format(delta, '+d') if delta is not None else '未知'}；"
                  f"本场已知累计：{inline(row.get('known_input_tokens_total'))}；"
                  f"用量缺失请求：{inline(row.get('missing_usage_requests'))}；消息数：{inline(row.get('message_count'))}")
    elif kind == "interaction_applied":
        if row.get("ended_turn"):
            detail = "已接收：结束行动"
            if row.get("pending_action"):
                detail += "；等待双方同步推进"
            elif "pending_action" in row:
                detail += "；本次行动已推进"
            else:
                detail += "；实际执行见裁判记录"
        elif row.get("silent"):
            detail = "已应用：保持沉默"
        else:
            detail = f"已应用：公开 {inline(row.get('message_count', len(row.get('event_sequences', []))))} 条发言"
        detail += f"；应用于 Tick {inline(row.get('applied_tick'))}"
    elif kind == "action_resolved":
        results = {"commit": "首次出拳已执行", "revise": "修改出拳已执行", "wait": "等待已执行", "move_ignored": "MOVE 已忽略（拳型锁定）"}
        detail = "裁判执行：" + "；".join(inline(results.get(item, item)) for item in row.get("outcomes", []))
        detail += f"；自己的拳型：{gesture(row.get('own_move'))}；执行于 Tick {inline(row.get('applied_tick'))}"
    elif kind == "action_discarded":
        detail = "行动未执行：" + inline(row.get("reason"))
    elif kind == "postmatch_applied":
        detail = "保持沉默，仍在场" if row.get("silent") else f"已公开 {len(row.get('event_sequences', []))} 条赛后事件"
        if row.get("exit"):
            detail += "；玩家退出"
    elif kind == "request_discarded":
        detail = "响应未应用：" + inline(row.get("reason", "对局结束或取消"))
    elif kind == "handoff":
        detail = f"交接完成：{inline(row.get('previous_player'))} → {author}；profile={inline(row.get('profile'))}"
        if "public_events" in row:
            detail += (f"\n  继承：{inline(row.get('completed_games'))} 个已完成局、{inline(row['public_events'])} 条公共事件、"
                       f"{inline(row.get('own_decisions'))} 次本席位分析与行动")
            detail += (f"\n  保留拳型：{gesture(row.get('own_move'))}；{'已锁定' if row.get('own_move_locked') else '未锁定'}；"
                       f"接管权限：{'可行动或聊天' if row.get('can_act') else '仅聊天'}")
            drafts = row.get("discarded_drafts", {})
            detail += f"\n  已丢弃草稿：出拳 {'有' if drafts.get('move') else '无'}、分析 {inline(drafts.get('analysis_count'))} 段"
    else:
        detail = inline(kind) + "：" + inline(json.dumps({key: value for key, value in row.items()
                    if key not in {"timestamp", "log_sequence", "kind", "actor"}}, ensure_ascii=False))
    return f"[{inline(row.get('timestamp', ''))}] {label}：{detail}"
