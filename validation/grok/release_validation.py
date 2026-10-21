"""Bounded live BO3 against a scripted peer, using the production Grok player."""

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import sys
import tomllib

import httpx

from model_rps.llm.adapters import MODEL_ADAPTERS, configure_thinking
from model_rps.core.game import Initiative
from model_rps.core.identities import player_names
from model_rps.core.logs import RunLogs
from model_rps.engine.match import MatchConfig, MatchStatus, run_match
from model_rps.players.base import ChatCompletionPlayer, ScriptedPlayer
from model_rps.engine.postmatch import MatchArchive, run_postmatch
from model_rps.core.prompts import GROK_STREAM_FORMAT_PROMPT
from model_rps.engine.requests import RequestPolicy


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--initiative', choices=['simultaneous', 'alternating'], default='alternating')
    args = parser.parse_args()
    original_config = Path('config.toml').read_bytes()
    config = tomllib.loads(original_config.decode('utf-8'))
    p = config['profiles']['grok']
    spec = configure_thinking(replace(MODEL_ADAPTERS['grok'], model=p['model'], base_url=p['base_url']), p.get('thinking', 'high'))
    names = player_names(spec.model, '验证脚本')
    counters = Counter()
    verdicts = Counter()
    gate = asyncio.Event()
    request_count = 0
    prompt_valid = True

    class Peer(ScriptedPlayer):
        def start_game(self, context):
            super().start_game(context)
            gate.clear()
            self.number = context['game']

        async def respond(self, view, feedback=None):
            if not view.can_act:
                return '{"actions":[]}'
            # 保持一个真实的聊天机会打开，等 Grok 完成聊天请求后再提交。
            if view.initiative == Initiative.SIMULTANEOUS or not view.opponent_committed:
                await gate.wait()
            move = ('Scissors', 'Rock', 'Paper')[(self.number - 1) % 3]
            return json.dumps({'actions': [{'type': 'ANALYZE', 'text': '按固定脚本选择拳型。'},
                                           {'type': 'MOVE', 'move': move}, {'type': 'ENDTURN'}]}, ensure_ascii=False)

        async def postmatch(self, context, feedback=None):
            return json.dumps({'actions': [{'type': 'SAY', 'text': '本场验证结束，感谢对局，我先退出了，再见。'},
                                           {'type': 'EXIT'}]}, ensure_ascii=False)

    async def before_request(request):
        nonlocal request_count, prompt_valid
        request_count += 1
        if request_count > 24:
            raise RuntimeError('Validation request budget exceeded')
        payload = json.loads(request.content)
        prompt_valid &= payload['messages'][0]['content'].count(GROK_STREAM_FORMAT_PROMPT) == 1

    async with httpx.AsyncClient(timeout=90, follow_redirects=False, event_hooks={'request': [before_request]}) as client:
        player = ChatCompletionPlayer(client, spec, p['api_key'], stream=True, names=names,
                                      max_output_tokens=20000, stream_first_timeout=60, stream_idle_timeout=30)
        peer = Peer()
        with RunLogs(Path(__file__).resolve().parents[2] / 'artifacts' / 'grok_validation' / 'v0.11.1', names=names,
                     secrets=tuple(x.get('api_key', '') for x in config['profiles'].values())) as logs:
            print('Evidence directory:', logs.path, flush=True)
            archive = MatchArchive()
            match_config = MatchConfig(best_of=3, max_games=5, initiative=args.initiative)
            policy = RequestPolicy(timeout=90, retry_delay=0)
            summary = {'initiative': args.initiative, 'best_of': 3, 'max_games': 5}

            def record(number, row):
                logs.request(number, row)
                archive.request(number, row)
                if row.get('player') != 'A':
                    return
                if row['kind'] == 'request_start':
                    counters[row.get('phase', 'game')] += 1
                if row['kind'] == 'request_end':
                    verdicts[row['status']] += 1
                    if row['status'] == 'valid' and row.get('phase') == 'chat':
                        gate.set()
                    print(json.dumps({'game': number, 'phase': row.get('phase'), 'attempt': row.get('attempt'),
                                      'status': row['status'], 'error': row.get('error'),
                                      'seconds': round(row.get('elapsed_seconds', 0), 2)}, ensure_ascii=False), flush=True)

            def finish(number, state, wins, draws):
                archive.finish_game(number, state)
                print(json.dumps({'game_finished': number, 'reason': state.result.reason,
                                  'wins': wins, 'draws': draws}, ensure_ascii=False), flush=True)

            async def idle(exited):
                summary['postmatch_idle'] = exited
                raise RuntimeError('Validation stopped at idle')

            try:
                async with asyncio.timeout(360):
                    result = await run_match(lambda: (player, peer), config=match_config, policy=policy,
                                             on_request=record, on_end=finish)
                    summary['match'] = {'status': result.status, 'wins': result.wins, 'draws': result.draws,
                                        'games_played': result.games_played}
                    if result.status != MatchStatus.TECHNICAL_ERROR:
                        outcome = await run_postmatch((player, peer), archive.report(match_config, result),
                            policy=policy, on_request=lambda row: record(0, row), on_idle=idle)
                        summary['postmatch'] = {'complete': outcome.complete, 'exited': outcome.exited, 'error': outcome.error}
            except Exception as exc:
                summary['stopped'] = type(exc).__name__
            summary.update(request_count=request_count, phases=dict(counters), verdicts=dict(verdicts),
                           all_requests_have_format_rule=prompt_valid,
                           config_unchanged=Path('config.toml').read_bytes() == original_config)
            (logs.path/'validation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
