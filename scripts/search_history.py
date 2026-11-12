#!/usr/bin/env python3
"""Return bounded, owner-scoped conversation search results as JSON."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.history_search import search
from assistant.service import read_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query', help='Keyword or phrase to find in active conversations')
    parser.add_argument('--page', type=int, default=1)
    args = parser.parse_args()
    try:
        # The trusted helper reads only the binding value for filtering. It never prints .env.
        binding = read_env(ROOT / '.env').get('FEISHU_OWNER_CHAT_ID')
        path = ROOT / '.local/messages.sqlite3'
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            result = search(db, binding, args.query, args.page)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f'历史对话检索失败：{type(error).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
