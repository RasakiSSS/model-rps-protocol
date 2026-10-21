"""Audit submitted payloads and save explicit manual factual-error annotations."""
import ast
from collections import Counter
import json
from pathlib import Path
import sys

from experiment import flip_messages

ROOT = Path(__file__).resolve().parents[2] / 'logs' / 'experiments' / 'position_swap_20261015'
RUN = Path((ROOT / 'latest.txt').read_text(encoding='utf-8'))
results = [json.loads(s) for s in (RUN / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
cases = json.loads((RUN / 'inputs.json').read_text(encoding='utf-8'))
annotations = {
    'gpt_g6_A_r2': ('history', '对手最近两局出布', '对手第4局为布，第5局为石头；并非两局均为布。'),
    'gpt_g6_B_r2': ('history', '最近两局双方都出石头', '第4局双方为布，第5局双方为石头。'),
    'deepseek_g6_A_r3': ('statistics', '我上局出石头后B通常出布压制(历史3/4次吻合)',
                         '已完成的“自己上一局石头→对手本局回应”只有G1→G2、G3→G4两次，均为布，实际2/2。G5的下一局尚未发生。'),
    'deepseek_g13_B_r2': ('history_sequence', '当我的上一局为石头时，A的历次回应依次为布、剪刀、布、剪刀交替',
                         '对应G2/G4/G6/G11/G12，对手回应依次为布、布、剪刀、布、剪刀；省略了第二个布，不能称历次严格交替。'),
}
ambiguous = {
    'deepseek_g13_A_r1': '称“近四局拳型呈剪刀、布、剪刀的交替趋势”，只列三个值；实际近四局是布、剪刀、布、剪刀。末三局与其列举相符，按保守标准仅记表达含混，不计明确错误。',
}
assert len(results) == 24 and len({r['id'] for r in results}) == 24
payloads = {}
statuses = Counter()
for identity in 'AB':
    rows = [json.loads(s) for s in (RUN / f'player_{identity.lower()}/debug.log').read_text(encoding='utf-8').splitlines()]
    for r in rows:
        if r['kind'] == 'http_request':
            assert r['experiment_case'] not in payloads
            payloads[r['experiment_case']] = r['payload']
        if r['kind'] == 'http_response':
            statuses[r['status']] += 1
assert len(payloads) == 24 and statuses == {200: 24}
for r in results:
    body = payloads[r['id']]
    case = cases[f"{r['provider']}_g{r['game']}_{r['identity']}"]
    assert body['messages'] == case['messages']
    assert body['reasoning_effort'] == ('medium' if r['provider'] == 'gpt' else 'high')
    assert body.get('max_tokens', body.get('max_completion_tokens')) == 20000
    if r['provider'] == 'deepseek':
        assert body['thinking'] == {'type': 'enabled'}
for provider in ('gpt', 'deepseek'):
    for game in (6, 13):
        assert flip_messages(cases[f'{provider}_g{game}_A']['messages']) == cases[f'{provider}_g{game}_B']['messages']
        for repeat in (1, 2, 3):
            a = payloads[f'{provider}_g{game}_A_r{repeat}']
            b = payloads[f'{provider}_g{game}_B_r{repeat}']
            assert {k:v for k,v in a.items() if k!='messages'} == {k:v for k,v in b.items() if k!='messages'}

scored = []
for r in results:
    note = annotations.get(r['id'])
    scored.append({**r, 'explicit_error': None if r['status'] != 'valid' else note is not None,
                   'error_category': note[0] if note else None, 'quote': note[1] if note else None,
                   'evidence': note[2] if note else None, 'ambiguous_note': ambiguous.get(r['id'])})
(RUN / 'scored_results.json').write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding='utf-8')
table = []
for provider in ('gpt', 'deepseek'):
    for identity in 'AB':
        group = [r for r in scored if r['provider']==provider and r['identity']==identity]
        table.append(f"| {provider} | {identity} | {len(group)} | {sum(r['status']=='valid' for r in group)} | {sum(r['explicit_error'] is True for r in group)} | {sum(r['status']!='valid' for r in group)} |")
report = '''# 相同历史 A/B 换位实验

结论：本次没有复现“错误稳定跟随 A 位”。GPT 与 DeepSeek 在 A、B 两位都各出现一份含明确事实错误的有效响应。样本不足以证明没有位置影响。

## 方法

使用 20261015_014708_111317_5bbf4e82 第6、13局的真实请求。每个模型使用自己的原始完整对话历史；同一模型同一快照只交换 A/B 标签及对应数据（包括自身此前说明中的身份标签），系统提示和消息顺序保持相同。每个位置重复3次，共24次，GPT medium、DeepSeek high，最大输出20000，每次超时1200秒。顺序在重复之间交替，两位模型并行，同一模型串行。每次独立请求，不推进比赛、不继承新输出、失败不补抽。

自动审计确认：每对请求除身份标签外全部一致，翻转两次恢复原输入；24个请求均返回HTTP 200。23个最终JSON通过校验，1个DeepSeek B位响应在说明文字中使用未转义双引号，未通过JSON解析，无网络失败。未修改正式游戏代码或默认配置。

## 最终决策说明判读

| 模型 | 位置 | 请求数 | 有效响应 | 含明确事实错误 | 格式失败 |
| --- | --- | --- | --- | --- | --- |
''' + '\n'.join(table) + '''

每份说明只计一次错误。预测、猜测或策略不佳不自动计错，未发现错误也不代表完全理解。按预先保存的 scoring.md 标准人工核对，不使用另一个模型自动打分。一次DeepSeek A位第13局对“近四局”只列三值的描述另记含混，未纳入明确错误计数。

明确错误及证据：

'''
for test_id, (category, quote, evidence) in annotations.items():
    report += f'- `{test_id}`：“{quote}”。实际：{evidence}\n'
report += '''
## 行动

GPT 第6局：A位三次为布、剪刀、剪刀；B位为石头、剪刀、剪刀。第13局两位六次均为布。DeepSeek所有11个有效响应均为剪刀。所有有效响应均只有MOVE，没有SAY或WAIT。

GPT有一对同次序号的输出不同，但在同一位置重复时也会变化，不能将该差异单独归因于身份。此实验不执行比赛，因此不计算胜率。

## 解读与限制

- 原先的第6局GPT“剪刀克制石头”错误未在这些重放中复现；相同位置、相同历史也能生成正确解释，说明原表现并非稳定必现。
- 目前没有足够证据认定A位有缺陷，也不能用本实验排除更弱的位置偏差。
- 每个模型只有两份历史、每个条件三个重复；原始历史中的自身错误说明仍保留。不同模型历史不同，因此主要比较同一模型内的换位差异，不能用此样本比较模型总体能力。
- 仅对身份标签进行置换，保留字段和事件顺序；它不单独隔离标签字母偏好、自己在记录中出现的先后顺序，也不测试运行调度器的先后影响。
- GPT使用原有第三方接口，结果只描述该服务实际响应，不能证明其内部模型与推理实现。
- 建议若继续验证，先扩展固定历史样本，并将标签、事件排序、历史中自身错误说明分别控制；不要在同一轮同时改提示词、模型档位和游戏规则。

原始输入见 inputs.json，请求和响应见 player_a/debug.log 与 player_b/debug.log，每次最终结果见 results.jsonl，逐条判读见 scored_results.json。这里的 player_a/player_b 日志按实验身份收纳两个提供方的请求，由 experiment_case 和 provider 区分，不能当作一场普通比赛日志。
'''
(RUN / 'report.md').write_text(report, encoding='utf-8')
keys = [n.value.value for n in ast.parse(Path('local_secrets.py').read_text(encoding='utf-8-sig')).body
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
        and any(isinstance(t, ast.Name) and 'API_KEY' in t.id for t in n.targets)]
assert not any(key and key.encode() in file.read_bytes() for file in RUN.rglob('*') if file.is_file() for key in keys)
sys.stdout.reconfigure(encoding='utf-8')
print('Payload pairing audit PASS; 24 responses HTTP200; known-key scan PASS')
print('\n'.join(table))
print(RUN / 'report.md')
