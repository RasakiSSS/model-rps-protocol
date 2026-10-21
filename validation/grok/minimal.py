"""Live fixed-output comparison; no game prompts or production changes."""

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
import difflib
import hashlib
import json
from pathlib import Path
import sys
import time
import tomllib

import httpx

from model_rps.llm.adapters import MODEL_ADAPTERS, auth_headers, configure_thinking, endpoint, request_payload
from model_rps.llm.streaming import CompletionStream, sse_data


TARGETS = {
    'single': '{"items":[{"x":1}]}',
    'multiple': '{"items":[{"x":1},{"x":2},{"x":3}]}',
    'multiline': json.dumps({'items': [{'x': 1}, {'x': 2}, {'x': 3}]}, indent=2),
    'nested': '{"a":{"x":1},"b":{"x":2}}',
    'spaced': '{"items": [ { "x": 1 }, { "x": 2 }, { "x": 3 } ]}',
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--patterns', nargs='+', choices=TARGETS, default=['single', 'multiple', 'multiline'])
    parser.add_argument('--modes', nargs='+', choices=['stream', 'nonstream'], default=['stream', 'nonstream'])
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 3:
        parser.error('--repeat must be between 1 and 3')
    config_bytes = Path('config.toml').read_bytes()
    config = tomllib.loads(config_bytes.decode('utf-8'))
    profile = config['profiles']['grok']
    secrets = [p['api_key'] for p in config['profiles'].values() if p.get('api_key')]
    def safe(text):
        for secret in sorted(secrets, key=len, reverse=True):
            for form in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
                text = text.replace(form, '[REDACTED]')
        return text
    spec = configure_thinking(replace(MODEL_ADAPTERS['grok'], model=profile['model'],
                                      base_url=profile['base_url']), profile.get('thinking', 'high'))
    output = Path(__file__).resolve().parents[2] / 'artifacts' / 'grok_validation' / ('minimal_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    output.mkdir()
    def save(name, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        (output / name).write_text(safe(text), encoding='utf-8')
    save('manifest.json', {'model': spec.model, 'base_url': spec.base_url, 'thinking': spec.thinking_mode,
                          'max_tokens': 20000, 'patterns': args.patterns, 'modes': args.modes, 'repeat': args.repeat})
    print('Evidence directory:', output, flush=True)
    semaphore = asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
        async def probe(pattern, mode, number):
            async with semaphore:
                case = f'{pattern}-{mode}-{number}'
                target = TARGETS[pattern]
                messages = [
                    {'role': 'system', 'content': 'Copy the JSON in the user message exactly, character for character. '
                         'Preserve its spaces and line breaks. Return only that JSON, without Markdown or explanation.'},
                    {'role': 'user', 'content': target},
                ]
                streaming = mode == 'stream'
                payload = request_payload(spec, messages, 20000, streaming)
                save(case + '-request.json', payload)
                summary = {'case': case, 'pattern': pattern, 'mode': mode, 'target': target,
                           'message_sha256': hashlib.sha256(json.dumps(messages).encode()).hexdigest()}
                raw = bytearray()
                started = time.monotonic()
                print('Starting:', case, flush=True)
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
                    summary['content'] = content
                    summary['exact_match'] = content == target
                    summary['usage'] = completion.get('usage')
                    summary['differences'] = [
                        {'operation': tag, 'target_range': [i, j], 'target_text': target[i:j], 'actual_text': content[a:b]}
                        for tag, i, j, a, b in difflib.SequenceMatcher(None, target, content).get_opcodes() if tag != 'equal'
                    ]
                    try:
                        actual = json.loads(content)
                        summary['json_valid'] = True
                        summary['data_matches_target'] = actual == json.loads(target)
                    except json.JSONDecodeError as exc:
                        summary.update(json_valid=False, data_matches_target=False, json_error=exc.msg, error_position=exc.pos)
                    return summary
                except Exception as exc:
                    summary['failure_type'] = type(exc).__name__
                    return summary
                finally:
                    summary['elapsed_seconds'] = round(time.monotonic() - started, 2)
                    save(case + '-raw.txt', bytes(raw).decode('utf-8', errors='replace'))
                    save(case + '-summary.json', summary)
                    print(safe(json.dumps({k: v for k, v in summary.items() if k != 'usage'}, ensure_ascii=False)), flush=True)
        results = await asyncio.gather(*(probe(pattern, mode, number) for pattern in args.patterns
                                        for mode in args.modes for number in range(1, args.repeat + 1)))
        save('results.json', results)
    print('config.toml unchanged:', Path('config.toml').read_bytes() == config_bytes, flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
