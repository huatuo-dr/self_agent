#!/usr/bin/env python3
"""Read owner-visible Feishu groups through the local service gateway."""

import argparse
import asyncio
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOCKET = ROOT / '.local/feishu-im-read.sock'


async def request(payload):
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(SOCKET), limit=2_100_000), 5)
    try:
        writer.write((json.dumps(payload, ensure_ascii=False) + '\n').encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), 50)
        return json.loads(raw)
    finally:
        writer.close()
        await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    search = sub.add_parser('search', help='Find a group by name')
    search.add_argument('--query', required=True)
    messages = sub.add_parser('messages', help='Read one page of group messages')
    messages.add_argument('--chat-id', required=True)
    messages.add_argument('--start')
    messages.add_argument('--end')
    messages.add_argument('--order', choices=('asc', 'desc'), default='asc')
    threads = sub.add_parser('threads', help='Read replies in one group thread')
    threads.add_argument('--thread-id', required=True)
    threads.add_argument('--order', choices=('asc', 'desc'), default='asc')
    for command in (search, messages, threads):
        command.add_argument('--page-size', type=int, default=50)
        command.add_argument('--page-token')
    args = parser.parse_args()
    payload = {key: value for key, value in vars(args).items() if value is not None}
    try:
        result = asyncio.run(request(payload))
    except (OSError, ValueError, asyncio.TimeoutError):
        result = {'ok': False, 'error': {'type': 'service_unavailable'}}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') is True else 1


if __name__ == '__main__':
    raise SystemExit(main())
