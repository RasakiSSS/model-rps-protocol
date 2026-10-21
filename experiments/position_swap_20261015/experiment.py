"""Frozen-history paired label experiment; no game state is advanced."""
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
from time import perf_counter

import httpx

from model_rps.cli import load_keys
from model_rps.diagnostics import request_trace
from model_rps.game import parse_decision
from model_rps.logs import RunLogs
from model_rps.players import ChatCompletionPlayer, DEEPSEEK, OPENAI, PlayerError, configure_thinking

ROOT = Path(__file__).resolve().parents[2] / 'logs' / 'experiments' / 'position_swap_20261015'
SOURCE = Path('logs/20261015_014708_111317_5bbf4e82')
LABEL = re.compile(r'(?<![A-Za-z])([AB])(?![A-Za-z])')


def swap(text):
    return LABEL.sub(lambda m: 'B' if m[1] == 'A' else 'A', text)


def flip_messages(messages):
    return [dict(m, content=swap(m['content'])) if m['role'] != 'system' else dict(m) for m in messages]


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def prepare():
    cases = {}
    for provider, original in [('gpt', 'A'), ('deepseek', 'B')]:
        data = rows(SOURCE / f'player_{original.lower()}/debug.log')
        for game in (6, 13):
            request = next(r for r in data if r['kind'] == 'http_request' and r['phase'] == 'game' and r['game'] == game)
            messages = request['payload']['messages']
            flipped = flip_messages(messages)
            assert flip_messages(flipped) == messages
            assert flipped[0] == messages[0]
            for identity in ('A', 'B'):
                candidate = messages if identity == original else flipped
                contexts = [json.loads(m['content']) for m in candidate if m['role'] == 'user']
                assert all(c['state']['player'] == identity for c in contexts)
                assert contexts[-1]['match']['game'] == game
                # Facts used for manual scoring are extracted from exactly the submitted history.
                facts = []
                for c in contexts:
                    last = c['match'].get('last_game')
                    if last:
                        revealed = {e['player']: e['text'] for e in last['events'] if e['kind'] == 'reveal'}
                        facts.append({'game': last['game'], 'self': revealed[identity],
                                      'opponent': revealed['B' if identity == 'A' else 'A']})
                cases[f'{provider}_g{game}_{identity}'] = {'messages': candidate, 'facts': facts,
                    'identity': identity, 'game': game, 'provider': provider}
    assert len({v['messages'][0]['content'] for v in cases.values()}) == 1
    return cases


async def main():
    keys = load_keys(Path('local_secrets.py'))
    cases = prepare()
    with RunLogs(ROOT / 'runs', secrets=keys) as logs:
        path = logs.path
        (ROOT / 'latest.txt').write_text(str(path.resolve()), encoding='utf-8')
        (path / 'inputs.json').write_text(json.dumps(logs.redact_value(cases), ensure_ascii=False, indent=2), encoding='utf-8')
        manifest = {'source': str(SOURCE), 'snapshots': [6, 13], 'repeats_per_identity': 3,
                    'thinking': {'gpt': 'medium', 'deepseek': 'high'}, 'max_output_tokens': 20000,
                    'timeout': 1200, 'planned_requests': 24, 'automatic_retries': 0,
                    'transformation': 'Swap A/B labels in all non-system messages, including keys and prior decision summaries; preserve order, prompts and all other content.',
                    'limitations': 'Different providers have their own fixed histories. Primary comparisons are within provider and snapshot. Existing erroneous assistant summaries remain fixed. No seed control; small exploratory sample.',
                    'prompt_sha256': hashlib.sha256(next(iter(cases.values()))['messages'][0]['content'].encode()).hexdigest()}
        (path / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print('EXPERIMENT', path.resolve(), flush=True)
        results = []
        with (path / 'results.jsonl').open('x', encoding='utf-8') as output:
            async with httpx.AsyncClient(timeout=1200, follow_redirects=False) as client:
                async def worker(provider, spec, key, mode):
                    for game in (6, 13):
                        for repeat in range(1, 4):
                            # Alternate order between replicates and providers.
                            order = ('A', 'B') if (repeat + (provider == 'deepseek')) % 2 else ('B', 'A')
                            for identity in order:
                                case = cases[f'{provider}_g{game}_{identity}']
                                test_id = f'{provider}_g{game}_{identity}_r{repeat}'
                                player = ChatCompletionPlayer(client, configure_thinking(spec, mode), key)
                                record = {'id': test_id, 'provider': provider, 'game': game, 'identity': identity, 'repeat': repeat}
                                started = perf_counter()
                                def handler(event):
                                    logs.request(game, {'experiment_case': test_id, 'provider': provider, **event})
                                with request_trace(handler, player=identity, tick=1, attempt=1, phase='experiment'):
                                    try:
                                        async with asyncio.timeout(1200):
                                            raw = await player._complete(deepcopy(case['messages']))
                                        decision = parse_decision(raw)
                                        record.update(status='valid', summary=decision.summary,
                                                      actions=[a.to_dict() for a in decision.actions])
                                    except (PlayerError, ValueError, TimeoutError) as exc:
                                        record.update(status='failed', error=type(exc).__name__)
                                record['elapsed_seconds'] = round(perf_counter() - started, 3)
                                record = logs.redact_value(record)
                                output.write(json.dumps(record, ensure_ascii=False) + '\n')
                                output.flush()
                                results.append(record)
                                print('DONE', test_id, record['status'], record['elapsed_seconds'], flush=True)
                await asyncio.gather(worker('gpt', OPENAI, keys[1], 'medium'),
                                     worker('deepseek', DEEPSEEK, keys[0], 'high'))
        print('COMPLETE', len(results), dict(Counter(r['status'] for r in results)), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
