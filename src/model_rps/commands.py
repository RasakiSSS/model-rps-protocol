"""玩家指令与阶段校验；JSON 和未来的终端输入共用同一种指令。"""

from dataclasses import dataclass
from enum import StrEnum
import json

from .game import Action, ActionKind, Gesture, InvalidResponse, validate_actions


class AnalysisPolicy(StrEnum):
    OFF = "off"
    OPTIONAL = "optional"
    REQUIRED = "required"


class CommandKind(StrEnum):
    SAY = "SAY"
    ANALYZE = "ANALYZE"
    MOVE = "MOVE"
    ENDTURN = "ENDTURN"
    EXIT = "EXIT"


@dataclass(frozen=True)
class Command:
    kind: CommandKind
    text: str | None = None
    move: Gesture | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CommandKind):
            raise InvalidResponse("指令 type 必须是 SAY、ANALYZE、MOVE、ENDTURN 或 EXIT。")
        if self.kind in (CommandKind.SAY, CommandKind.ANALYZE):
            if not isinstance(self.text, str) or not self.text.strip() or self.move is not None:
                raise InvalidResponse(f"{self.kind} 需要非空字符串 text，不能包含 move。")
        elif self.kind == CommandKind.MOVE:
            if not isinstance(self.move, Gesture) or self.text is not None:
                raise InvalidResponse("MOVE 需要 Rock、Paper 或 Scissors，不能包含 text。")
        elif self.text is not None or self.move is not None:
            raise InvalidResponse(f"{self.kind} 不接受参数。")

    def to_dict(self) -> dict:
        result = {"type": self.kind.value}
        if self.text is not None:
            result["text"] = self.text
        if self.move is not None:
            result["move"] = self.move.value
        return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponse("JSON 对象不能包含重复字段。")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise InvalidResponse("JSON 不能包含 NaN 或 Infinity。")


def parse_commands(raw: str, *, allow_empty: bool = False) -> tuple[Command, ...]:
    """整份校验后才返回；不执行任何命令，不将纯聊天隐式结束为等待。"""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidResponse("响应不能为空，请返回完整 JSON 对象。")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError):
        raise InvalidResponse("响应必须是完整 JSON，不要添加 Markdown 标记或解释文字。") from None
    if not isinstance(payload, dict) or set(payload) != {"actions"}:
        raise InvalidResponse("响应必须是只包含 actions 字段的 JSON 对象。")
    if not isinstance(payload["actions"], list) or (not payload["actions"] and not allow_empty):
        raise InvalidResponse("actions 必须是非空指令列表。")
    commands = []
    for item in payload["actions"]:
        if not isinstance(item, dict):
            raise InvalidResponse("每条指令必须是 JSON 对象。")
        try:
            kind = CommandKind(item.get("type"))
        except (ValueError, TypeError):
            raise InvalidResponse("指令 type 必须是 SAY、ANALYZE、MOVE、ENDTURN 或 EXIT。") from None
        expected = {"type", "move"} if kind == CommandKind.MOVE else (
            {"type"} if kind in (CommandKind.ENDTURN, CommandKind.EXIT) else {"type", "text"})
        if set(item) != expected:
            raise InvalidResponse(f"{kind} 字段不正确，请按规定的指令格式返回。")
        move = None
        if kind == CommandKind.MOVE:
            try:
                move = Gesture(item["move"])
            except (ValueError, TypeError):
                raise InvalidResponse("move 必须是 Rock、Paper 或 Scissors。") from None
        commands.append(Command(kind, text=item.get("text"), move=move))
    return tuple(commands)


def parse_command_line(line: str) -> Command:
    """仅解析单条 /指令，尚不提供交互终端或自动结束行动。"""
    parts = line.strip().split(maxsplit=1) if isinstance(line, str) else []
    if not parts or not parts[0].startswith("/"):
        raise InvalidResponse("请输入 /say、/analyze、/move、/endturn 或 /exit。")
    try:
        kind = CommandKind(parts[0][1:].upper())
    except ValueError:
        raise InvalidResponse("未知指令，请使用 /say、/analyze、/move、/endturn 或 /exit。") from None
    argument = parts[1] if len(parts) == 2 else None
    if kind == CommandKind.MOVE:
        try:
            return Command(kind, move=Gesture(argument.capitalize() if argument else argument))
        except ValueError:
            raise InvalidResponse("/move 参数必须是 Rock、Paper 或 Scissors。") from None
    return Command(kind, text=argument)


@dataclass(frozen=True)
class Decision:
    summary: str | None
    actions: tuple[Action, ...]


def validate_decision(commands: tuple[Command, ...], *,
                      analysis: AnalysisPolicy = AnalysisPolicy.REQUIRED) -> Decision:
    """当前 Tick 调度器要求完整行动；ENDTURN 是结束标记，MOVE 自身负责提交。"""
    analysis = AnalysisPolicy(analysis)
    if any(c.kind == CommandKind.EXIT for c in commands):
        raise InvalidResponse("比赛进行中不能提交 EXIT。")
    if not commands or commands[-1].kind != CommandKind.ENDTURN or sum(
        c.kind == CommandKind.ENDTURN for c in commands
    ) != 1:
        raise InvalidResponse("完整行动必须以唯一的 ENDTURN 结束；纯聊天不会结束行动。")
    texts = [c.text for c in commands if c.kind == CommandKind.ANALYZE]
    if analysis == AnalysisPolicy.OFF and texts:
        raise InvalidResponse("本席位已关闭分析，请移除 ANALYZE。")
    if analysis == AnalysisPolicy.REQUIRED and not texts:
        raise InvalidResponse("本席位要求策略分析，请通过 ANALYZE 提交非空文本；接口思考不能替代该指令。")
    actions = tuple(Action(ActionKind(c.kind.value), text=c.text, move=c.move) for c in commands
                    if c.kind in (CommandKind.SAY, CommandKind.MOVE))
    if sum(c.kind == CommandKind.MOVE for c in commands) > 1:
        raise InvalidResponse("每次完整行动最多提交一个 MOVE。")
    if not any(action.kind == ActionKind.MOVE for action in actions):
        actions += (Action(ActionKind.WAIT),)
    validate_actions(actions)
    return Decision("\n\n".join(texts) if texts else None, actions)


def parse_decision(raw: str, *, analysis: AnalysisPolicy = AnalysisPolicy.REQUIRED) -> Decision:
    return validate_decision(parse_commands(raw), analysis=analysis)


def parse_response(raw: str) -> tuple[Action, ...]:
    return parse_decision(raw).actions


@dataclass(frozen=True)
class Interaction:
    """一次完整响应：公开聊天或完整行动，不把响应结束等同于行动结束。"""

    messages: tuple[str, ...]
    decision: Decision | None = None


def parse_interaction(raw: str, *, can_act: bool,
                      analysis: AnalysisPolicy = AnalysisPolicy.REQUIRED) -> Interaction:
    commands = parse_commands(raw, allow_empty=not can_act)
    messages = tuple(c.text for c in commands if c.kind == CommandKind.SAY)
    if all(c.kind == CommandKind.SAY for c in commands):
        return Interaction(messages)
    if not can_act:
        raise InvalidResponse("本次仅有聊天权限，只能返回 SAY；不回复请返回 {\"actions\":[]}。")
    return Interaction(messages, validate_decision(commands, analysis=analysis))


@dataclass(frozen=True)
class PostmatchResponse:
    messages: tuple[str, ...]
    exit: bool = False


def parse_postmatch(raw: str) -> PostmatchResponse:
    commands = parse_commands(raw)
    if any(c.kind not in (CommandKind.SAY, CommandKind.EXIT) for c in commands):
        raise InvalidResponse("赛后只能 SAY 发言或 EXIT 退出，不接受游戏动作。")
    exits = [i for i, command in enumerate(commands) if command.kind == CommandKind.EXIT]
    if exits and exits != [len(commands) - 1]:
        raise InvalidResponse("EXIT 最多一次，且必须是最后一条指令。")
    return PostmatchResponse(tuple(c.text for c in commands if c.kind == CommandKind.SAY), bool(exits))
