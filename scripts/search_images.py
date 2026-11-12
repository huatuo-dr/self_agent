#!/usr/bin/env python3
"""List the owner's archived pictures without reading their image bytes."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.image_archive import search
from assistant.service import read_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=int, help='Filter by session ID')
    parser.add_argument('--message-id', help='Find one Feishu message ID')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    try:
        cfg = read_env(ROOT / '.env')
        binding = cfg.get('FEISHU_OWNER_CHAT_ID')
        path = ROOT / '.local/messages.sqlite3'
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            items = search(db, binding, ROOT, args.session, args.message_id, args.limit, cfg=cfg)
        print(json.dumps({'items': items, 'count': len(items)}, ensure_ascii=False))
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f'历史图片检索失败：{type(error).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
