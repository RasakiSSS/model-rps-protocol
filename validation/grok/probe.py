"""Bounded, opt-in live comparison. Credentials stay in config.toml."""

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
import tomllib

import httpx

from model_rps.llm.adapters import MODEL_ADAPTERS, auth_headers, configure_thinking, endpoint, request_payload
from model_rps.core.commands import AnalysisPolicy, parse_interaction
from model_rps.core.game import GameState, Initiative, MovePolicy
from model_rps.players.history import ConversationHistory
from model_rps.core.identities import named_data, player_names
from model_rps.core.prompts import game_prompt
from model_rps.llm.streaming import CompletionStream, sse_data


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', nargs='+', default=['stream', 'nonstream'])
    args = parser.parse_args()
    config = tomllib.loads(Path('config.toml').read_text(encoding='utf-8'))
    profile = config['profiles']['grok']
    secrets = [p['api_key'] for p in config['profiles'].values() if p.get('api_key')]
    def safe(text):
        for secret in sorted(secrets, key=len, reverse=True):
            for form in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
                text = text.replace(form, '[REDACTED]')
        return text
    spec = configure_thinking(replace(MODEL_ADAPTERS['grok'], model=profile['model'],
                                      base_url=profile['base_url']), profile.get('thinking', 'high'))
    output = Path(__file__).resolve().parents[2] / 'logs' / '_grok_validation' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    output.mkdir()
    def save(name, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        (output / name).write_text(safe(text), encoding='utf-8')
    names = player_names(spec.model, 'test-opponent')
    view = GameState(move_policy=MovePolicy.LOCKED, initiative=Initiative.ALTERNATING).view_for('A')
    prompt = game_prompt(view.move_policy, view.initiative, AnalysisPolicy.REQUIRED)
    match = {'game': 1, 'best_of': 1, 'target_wins': 1, 'max_games': 3, 'wins': {'A': 0, 'B': 0}, 'draws': 0}
    context = ConversationHistory(prompt).game_context(view, match, None)
    messages = [{'role': 'system', 'content': prompt},
                {'role': 'user', 'content': json.dumps(named_data(context, names), ensure_ascii=False)}]
    print('Evidence directory:', output, flush=True)
    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
        async def probe(case):
            streaming = case != 'nonstream'
            case_messages = [dict(message) for message in messages]
            if case.startswith('stream-pretty'):
                case_messages[0]['content'] += ('\n输出 JSON 使用缩进换行格式，每个对象的左花括号后先换行，'
                    '字段名放到下一行；不要使用紧凑单行 JSON。只改变空白，不改变指令字段或规则。')
            payload = request_payload(spec, case_messages, 20000, streaming)
            if case == 'stream-no-format':
                payload.pop('response_format')
            if case == 'stream-no-usage':
                payload.pop('stream_options')
            if case == 'stream-schema':
                def obj(properties):
                    return {'type': 'object', 'properties': properties,
                            'required': list(properties), 'additionalProperties': False}
                action = {'anyOf': [obj({'type': {'type': 'string', 'enum': [kind]}, **fields})
                    for kind, fields in [('SAY', {'text': {'type': 'string'}}),
                                         ('ANALYZE', {'text': {'type': 'string'}}),
                                         ('MOVE', {'move': {'type': 'string', 'enum': ['Rock', 'Paper', 'Scissors']}}),
                                         ('ENDTURN', {}), ('EXIT', {})]]}
                payload['response_format'] = {'type': 'json_schema', 'json_schema': {
                    'name': 'player_response', 'strict': True,
                    'schema': obj({'actions': {'type': 'array', 'items': action}})}}
            save(case + '-request.json', payload)
            summary = {'case': case, 'model': spec.model, 'thinking': spec.thinking_mode,
                       'message_sha256': hashlib.sha256(json.dumps(case_messages).encode()).hexdigest()}
            raw = bytearray()
            started = time.monotonic()
            try:
                async with asyncio.timeout(120):
                    async with client.stream('POST', endpoint(spec), headers=auth_headers(spec, profile['api_key']),
                                             json=payload) as response:
                        summary['http_status'] = response.status_code
                        summary['content_type'] = response.headers.get('content-type')
                        async for data in response.aiter_bytes():
                            raw.extend(data)
                body = bytes(raw).decode('utf-8')
                if summary['http_status'] != 200:
                    summary['result'] = 'http_error'
                    return summary
                if streaming:
                    # Independent SSE extraction, without the application's decoder or concatenator.
                    events = []
                    for group in body.replace('\r\n', '\n').replace('\r', '\n').split('\n\n'):
                        values = [line[5:].removeprefix(' ') for line in group.split('\n') if line.startswith('data:')]
                        if values:
                            events.append('\n'.join(values))
                    chunks = [json.loads(event) for event in events if event.strip() != '[DONE]']
                    deltas = [(chunk.get('choices') or [{}])[0].get('delta', {}).get('content') for chunk in chunks]
                    independent = ''.join(value for value in deltas if isinstance(value, str))
                    summary['done'] = any(event.strip() == '[DONE]' for event in events)
                    summary['finish_reasons'] = [choice['finish_reason'] for chunk in chunks
                        for choice in chunk.get('choices', []) if choice.get('finish_reason') is not None]
                    summary['event_count'] = len(events)
                    assembled = CompletionStream()
                    replay = httpx.Response(200, content=bytes(raw), headers={'content-type': 'text/event-stream'})
                    try:
                        async for event in sse_data(replay):
                            assembled.feed(event)
                            if assembled.done:
                                break
                        completion = assembled.result()
                        summary['application_parser'] = 'passed'
                    except Exception as exc:
                        summary['application_parser'] = type(exc).__name__ + ': ' + str(exc)
                        completion = {'choices': [{'message': assembled.message}], 'usage': assembled.usage}
                    content = completion['choices'][0]['message'].get('content') or ''
                    summary['independent_matches_application'] = independent == content
                    save(case + '-content-deltas.json', deltas)
                else:
                    completion = json.loads(body)
                    content = completion['choices'][0]['message'].get('content') or ''
                    summary['finish_reasons'] = [completion['choices'][0].get('finish_reason')]
                save(case + '-content.txt', content)
                summary['content_length'] = len(content)
                summary['usage'] = completion.get('usage')
                try:
                    json.loads(content)
                    summary['json_valid'] = True
                except json.JSONDecodeError as exc:
                    summary.update(json_valid=False, json_error=exc.msg, error_position=exc.pos,
                                   error_excerpt=content[max(0, exc.pos-100):exc.pos+100])
                try:
                    interaction = parse_interaction(content, can_act=True, analysis=AnalysisPolicy.REQUIRED)
                    summary['commands_valid'] = True
                    summary['has_decision'] = interaction.decision is not None
                except ValueError as exc:
                    summary['commands_valid'] = False
                    summary['validation_error'] = str(exc)
                return summary
            except Exception as exc:
                # Do not print HTTP exception text, which can contain request details.
                summary['failure_type'] = type(exc).__name__
                return summary
            finally:
                summary['elapsed_seconds'] = round(time.monotonic() - started, 2)
                save(case + '-raw.txt', bytes(raw).decode('utf-8', errors='replace'))
                save(case + '-summary.json', summary)
                print(safe(json.dumps(summary, ensure_ascii=False)), flush=True)
        await asyncio.gather(*(probe(case) for case in args.cases))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
