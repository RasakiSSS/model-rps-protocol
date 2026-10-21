import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import complete_public_report, turn_payload, postmatch_payload
from model_rps.cli import main, make_parser
from model_rps.core.commands import (AnalysisPolicy, CommandKind, parse_commands, parse_command_line,
                                parse_decision, validate_decision, parse_postmatch)
from model_rps.config import RuntimeConfig, load_config
from model_rps.core.game import ActionKind, InvalidResponse


class CommandTests(unittest.TestCase):
    def test_text_and_json_inputs_share_validation_and_private_analysis(self):
        lines = ['/analyze 第一段\n第二段', '/say 你先来', '/move rock', '/endturn']
        commands = tuple(parse_command_line(line) for line in lines)
        raw = json.dumps({'actions': [command.to_dict() for command in commands]})
        self.assertEqual(parse_commands(raw), commands)
        decision = validate_decision(commands)
        self.assertEqual(decision.summary, '第一段\n第二段')
        self.assertEqual([a.kind for a in decision.actions], [ActionKind.SAY, ActionKind.MOVE])
        self.assertNotIn('第一段', repr(decision.actions))
        goodbye = [parse_command_line('/say 下次再玩'), parse_command_line('/exit')]
        response = parse_postmatch(json.dumps({'actions': [c.to_dict() for c in goodbye]}))
        self.assertEqual(response.messages, ('下次再玩',))
        self.assertTrue(response.exit)

    def test_chat_does_not_implicitly_end_turn_or_require_analysis(self):
        commands = parse_commands('{"actions":[{"type":"SAY","text":"聊天"}]}')
        self.assertEqual(commands[0].kind, CommandKind.SAY)
        for policy in AnalysisPolicy:
            with self.subTest(policy=policy), self.assertRaisesRegex(InvalidResponse, 'ENDTURN'):
                validate_decision(commands, analysis=policy)
        ended = parse_decision('{"actions":[{"type":"SAY","text":"聊天"},{"type":"ENDTURN"}]}',
                               analysis=AnalysisPolicy.OPTIONAL)
        self.assertEqual([a.kind for a in ended.actions], [ActionKind.SAY, ActionKind.WAIT])

    def test_analysis_policy_multiple_paragraphs_and_per_turn_requirement(self):
        empty = '{"actions":[{"type":"ENDTURN"}]}'
        provided = json.dumps(turn_payload([{'type': 'ANALYZE', 'text': '补充分析'}], '第一段'))
        for policy in (AnalysisPolicy.OPTIONAL, AnalysisPolicy.OFF):
            self.assertIsNone(parse_decision(empty, analysis=policy).summary)
        with self.assertRaisesRegex(InvalidResponse, 'ANALYZE'):
            parse_decision(empty)
        with self.assertRaisesRegex(InvalidResponse, '关闭分析'):
            parse_decision(provided, analysis=AnalysisPolicy.OFF)
        self.assertEqual(parse_decision(provided).summary, '第一段\n\n补充分析')
        # 解析器不继承上次已提供的分析。
        with self.assertRaises(InvalidResponse):
            parse_decision(empty)

    def test_strict_command_fields_json_and_phase_boundaries(self):
        invalid = [
            '{"actions":[{"type":"ENDTURN"}],"actions":[{"type":"ENDTURN"}]}',
            '{"actions":[{"type":"SAY","type":"ANALYZE","text":"x"}]}',
            '{"actions":[{"type":"SAY","text":NaN}]}',
            '{"actions":[{"type":"SAY","text":Infinity}]}',
            '{"actions":[{"type":[]}]}', '{"actions":[{"type":"WAIT"}]}',
            '{"decision_summary":"old","actions":[{"type":"ENDTURN"}]}',
        ]
        for action in ({'type': 'ENDTURN', 'text': 'extra'}, {'type': 'MOVE', 'move': 'Rock', 'text': 'x'},
                       {'type': 'REVIEW', 'text': ''}, {'type': 'ANALYZE', 'text': []},
                       {'type': 'SAY', 'text': None}):
            invalid.append(json.dumps({'actions': [action]}))
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(InvalidResponse):
                parse_commands(raw)
        for actions in ([{'type': 'ENDTURN'}, {'type': 'SAY', 'text': 'late'}],
                        [{'type': 'ENDTURN'}, {'type': 'ENDTURN'}],
                        [{'type': 'MOVE', 'move': 'Rock'}, {'type': 'MOVE', 'move': 'Paper'}],
                        [{'type': 'EXIT'}]):
            with self.subTest(actions=actions), self.assertRaises(InvalidResponse):
                parse_decision(json.dumps(turn_payload(actions)))
        for kind in ('ANALYZE', 'MOVE', 'ENDTURN'):
            commands = [{'type': kind, 'move': 'Rock'}] if kind == 'MOVE' else (
                [{'type': kind}] if kind == 'ENDTURN' else [{'type': kind, 'text': 'not postmatch'}])
            with self.subTest(kind=kind), self.assertRaises(InvalidResponse):
                parse_postmatch(json.dumps({'actions': commands}))
        with self.assertRaises(InvalidResponse):
            parse_postmatch(json.dumps({'actions': [{'type': 'SAY', 'text': 'ok'}, {'type': 'ENDTURN'}]}))
        self.assertEqual(parse_postmatch(json.dumps(postmatch_payload('自由讨论'))).messages, ('自由讨论',))

    def test_invalid_terminal_command_is_not_accepted(self):
        for line in ('say hello', '/wait', '/next', '/move', '/move Lizard', '/move Rock Paper',
                     '/say', '/analyze  ', '/review', '/exit extra', '/endturn extra', ''):
            with self.subTest(line=line), self.assertRaises(InvalidResponse):
                parse_command_line(line)

    def test_config_precedence_and_invalid_policy_fail_before_network(self):
        defaults = RuntimeConfig.from_args(make_parser().parse_args([]))
        self.assertEqual((defaults.red.analysis, defaults.blue.analysis), ('required', 'required'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[players.red]\nanalysis="off"\n[players.blue]\nanalysis="optional"', encoding='utf-8')
            args = make_parser(load_config(path)).parse_args(['--red-analysis', 'required'])
            config = RuntimeConfig.from_args(args)
            self.assertEqual((config.red.analysis, config.blue.analysis), ('required', 'optional'))
            for value in ('"automatic"', 'false', '1'):
                path.write_text('[players.red]\nanalysis=' + value, encoding='utf-8')
                with patch('model_rps.cli.httpx.AsyncClient') as client, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(['--config', str(path)]), 2)
                client.assert_not_called()

    def test_per_seat_analysis_policies_reach_prompts_validation_and_postmatchs(self):
        for initiative, rule in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
            with self.subTest(initiative=initiative, rule=rule), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / 'config.toml'
                path.write_text('[app]\nretry_delay_seconds=0\n'
                                f'[match]\ninitiative="{initiative}"\nmove_policy="{rule}"\n'
                                '[players.red]\nanalysis="off"\napi_key="red-key"\n'
                                '[players.blue]\nanalysis="optional"\napi_key="blue-key"', encoding='utf-8')
                calls, postmatchs = {'Red': 0, 'Blue': 0}, {}

                def handler(request):
                    body = json.loads(request.content)
                    context = json.loads(body['messages'][-1]['content'])
                    seat = context.get('player', context.get('state', {}).get('player')).split(' · ')[0]
                    if context['phase'] == 'postmatch':
                        postmatchs[seat] = body
                        content = postmatch_payload('自由复盘\n保留换行')
                    else:
                        calls[seat] += 1
                        self.assertIn('关闭分析' if seat == 'Red' else '分析可选', body['messages'][0]['content'])
                        actions = [{'type': 'MOVE', 'move': 'Paper' if seat == 'Red' else 'Rock'}, {'type': 'ENDTURN'}]
                        # 关闭分析时即使正文和接口思考都有文本，也必须按该席位规则重试。
                        if seat == 'Red' and calls[seat] == 1:
                            actions.insert(0, {'type': 'ANALYZE', 'text': 'REJECTED-ANALYSIS'})
                        content = {'actions': actions}
                    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                        'content': json.dumps(content, ensure_ascii=False), 'reasoning_content': 'NATIVE-PRIVATE'}}]})

                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with patch('model_rps.cli.httpx.AsyncClient', return_value=client), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(['--config', str(path), '--log-dir', str(root / 'logs')]), 0)
                self.assertEqual(calls['Red'], calls['Blue'] + 1)
                self.assertEqual(set(postmatchs), {'Red', 'Blue'})
                self.assertEqual(complete_public_report(postmatchs['Red']['messages']),
                                 complete_public_report(postmatchs['Blue']['messages']))
                self.assertIn('REJECTED-ANALYSIS', json.dumps(postmatchs['Red']))
                self.assertNotIn('REJECTED-ANALYSIS', json.dumps(postmatchs['Blue']))
                self.assertNotIn('NATIVE-PRIVATE', json.dumps(postmatchs))
                self.assertNotIn('REJECTED-ANALYSIS', output.getvalue())
                self.assertIn('本次未提交策略分析', output.getvalue())
                self.assertIn('自由复盘\n  保留换行', output.getvalue())

    def test_demo_off_policy_keeps_all_scenarios_usable(self):
        for scenario in ('locked', 'change', 'draw', 'settlement', 'forfeit', 'deadline', 'retry'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                with contextlib.redirect_stdout(io.StringIO()):
                    status = main(['--no-config', '--mode', 'demo', '--scenario', scenario,
                                   '--move-policy', 'revisable' if scenario in ('change', 'settlement') else 'locked',
                                   '--red-analysis', 'off', '--blue-analysis', 'off',
                                   '--max-games', '1', '--log-dir', directory])
                self.assertEqual(status, 3 if scenario == 'draw' else 0)


if __name__ == '__main__':
    unittest.main()
