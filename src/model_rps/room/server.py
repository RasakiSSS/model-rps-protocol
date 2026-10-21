"""本机共享房间 HTTP 服务。HTTP 只承载玩家命令，比赛仍在单个 asyncio 循环内推进。"""

import argparse
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from ..core.game import InvalidResponse
from ..engine.match import MatchConfig
from .shared import SharedRoom
from .service import RoomService


class RoomHTTPServer:
    def __init__(self, room, port=8765):
        self.port = port
        self.service = RoomService(room)

    @property
    def room(self):
        return self.service.current

    async def __aenter__(self):
        loop, room = asyncio.get_running_loop(), self.service

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # 不把 URL、凭证或请求正文写到 HTTP 访问日志。

            def setup(self):
                super().setup()
                self.connection.settimeout(30)

            def respond(self, status, payload):
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
                self.close_connection = True
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 65536:
                        raise ValueError
                    data = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError, OSError):
                    return self.respond(400, {'error': '请求必须为不超过 64 KiB 的完整 JSON。'})
                # 先消费有界正文再返回错误，避免 Windows 在关闭未读 socket 时发送 RST。
                if self.headers.get('Origin') or self.headers.get('Transfer-Encoding'):
                    return self.respond(403, {'error': '不接受浏览器或分块请求。'})
                auth = self.headers.get('Authorization', '')
                identity = room.identity(auth[7:]) if auth.startswith('Bearer ') else None
                if identity is None:
                    return self.respond(401, {'error': '席位凭证无效。'})
                if self.path not in ('/info', '/join', '/state', '/wait', '/act', '/ping', '/leave'):
                    return self.respond(404, {'error': '未知接口。'})
                future = asyncio.run_coroutine_threadsafe(room.dispatch(identity, self.path[1:], data), loop)
                try:
                    self.respond(200, future.result(timeout=25))
                except (InvalidResponse, ValueError) as exc:
                    self.respond(400, {'error': str(exc)})
                except FutureTimeout:
                    future.cancel()
                    self.respond(503, {'error': '房间请求超时。'})
                except Exception:
                    self.respond(500, {'error': '房间服务发生内部异常。'})

        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        self.thread.start()
        self.monitor = asyncio.create_task(room.monitor())
        return self

    async def __aexit__(self, *args):
        self.monitor.cancel()
        await asyncio.gather(self.monitor, return_exceptions=True)
        await self.service.close()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        await asyncio.to_thread(self.thread.join)


async def serve(args):
    from .terminal import RoomTerminal

    room = SharedRoom(config=MatchConfig(args.bo, args.max_games, args.move_policy, args.initiative),
                      analysis=(args.red_analysis, args.blue_analysis), log_dir=args.log_dir)
    async with RoomHTTPServer(room, args.port) as server:
        await RoomTerminal(server).run()


def main(argv=None):
    parser = argparse.ArgumentParser(description='启动本机共享 RPS 房间，两个终端或 MCP Agent 可加入同一场比赛。')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--bo', type=int, default=3)
    parser.add_argument('--max-games', type=int)
    parser.add_argument('--initiative', choices=('simultaneous', 'alternating'), default='simultaneous')
    parser.add_argument('--move-policy', choices=('locked', 'revisable'), default='locked')
    for seat in ('red', 'blue'):
        parser.add_argument(f'--{seat}-analysis', choices=('off', 'optional', 'required'), default='optional')
    parser.add_argument('--log-dir', type=Path, default=Path('logs'))
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.port <= 65535:
            raise ValueError('端口必须在 0 到 65535 之间。')
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError):
        print('房间启动失败，请检查比赛参数、日志目录和端口是否被占用。')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
