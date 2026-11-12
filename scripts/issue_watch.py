#!/usr/bin/env python3
"""Read-only diagnostics for the Issue skill; never sends notifications."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.service import config, database, run_command, STATE
from assistant.issues import IssueAssistant, now_local, is_workday


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['query', 'workday', 'status'])
    args = parser.parse_args()
    cfg = config()
    if not cfg.get('GITLAB_SKILL_ENABLED') or not cfg.get('GITLAB_HOST'):
        parser.error('Configure GITLAB_SKILL_ENABLED and GITLAB_HOST first')
    STATE.mkdir(mode=0o700, exist_ok=True)
    db = database(STATE / 'messages.sqlite3')
    try:
        issues = IssueAssistant(cfg, db, run_command, ROOT, STATE)
        if args.command == 'query':
            items = await issues.fetch_all()
            print(issues.format_items(items, '当前全部待办（诊断查询，不发送、不加入池）'))
        elif args.command == 'workday':
            day = now_local(cfg).date()
            calendar = await issues.calendar(day)
            print(json.dumps({'date': day.isoformat(), 'workday': is_workday(day, calendar),
                              'mode': calendar['mode']}, ensure_ascii=False))
        else:
            print(json.dumps({'enabled': cfg.get('ISSUE_SKILL_ENABLED', False),
                              'pool_size': db.execute('SELECT count(*) FROM issue_pool WHERE scope=?', (issues.scope,)).fetchone()[0],
                              'days': db.execute('SELECT day,workday,reset,last_slot FROM issue_days WHERE scope=? ORDER BY day DESC LIMIT 3', (issues.scope,)).fetchall()}, ensure_ascii=False))
    finally:
        db.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (RuntimeError, ValueError, OSError, asyncio.TimeoutError) as error:
        print(f'诊断失败：{error}', file=sys.stderr)
        sys.exit(1)
