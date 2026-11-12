"""Durable GitLab Issue/MR comment watches for the bound owner."""
import asyncio
from datetime import datetime
import hashlib
import json
import logging
import re
import time
from urllib.parse import quote, urlsplit

from assistant.issues import parse_ref
from assistant.settings import get_timezone


def response_schema():
    return {'type': 'array', 'maxItems': 20, 'items': {
        'type': 'object', 'properties': {
            'url': {'type': 'string'},
            'event': {'type': 'string', 'enum': ['new_comment']},
            'interval_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440},
            'mode': {'type': 'string', 'enum': ['once', 'continuous']}},
        'required': ['url', 'event', 'interval_minutes', 'mode'],
        'additionalProperties': False}}


def target_endpoint(url, host):
    parts = urlsplit(url) if isinstance(url, str) else None
    if (not parts or parts.hostname != host or parts.query or parts.fragment
            or parts.port or parts.username or parts.password):
        raise ValueError('请提供当前 GitLab 主机上的完整 Issue/MR 链接。')
    parsed_host, project, kind, iid = parse_ref(url)
    if parsed_host != host or iid < 1:
        raise ValueError('请提供当前 GitLab 主机上的完整 Issue/MR 链接。')
    endpoint = f'projects/{quote(project, safe="")}/{kind}/{iid}'
    return kind, iid, endpoint


def flatten_notes(discussions):
    if not isinstance(discussions, list):
        raise ValueError('GitLab 讨论列表格式无效。')
    result = []
    for discussion in discussions:
        if not isinstance(discussion, dict) or not isinstance(discussion.get('notes'), list):
            raise ValueError('GitLab 讨论列表格式无效。')
        for note in discussion['notes']:
            if (not isinstance(note, dict) or type(note.get('id')) is not int
                    or note['id'] < 1 or type(note.get('system')) is not bool):
                raise ValueError('GitLab 评论格式无效。')
            result.append(note)
    return result


class TargetWatch:
    def __init__(self, cfg, db, run):
        self.cfg, self.db, self.run = cfg, db, run
        self.binding = cfg['FEISHU_OWNER_CHAT_ID']
        self.host = cfg['GITLAB_HOST']
        db.executescript('''
            CREATE TABLE IF NOT EXISTS target_watches (
                id TEXT PRIMARY KEY, binding TEXT NOT NULL, session_id INTEGER,
                source TEXT UNIQUE NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
                event TEXT NOT NULL, interval_minutes INTEGER NOT NULL, mode TEXT NOT NULL,
                baseline_id INTEGER NOT NULL, next_check REAL NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                checked_at REAL NOT NULL DEFAULT 0, notified_at REAL NOT NULL DEFAULT 0,
                last_result TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS target_watches_due
                ON target_watches(binding,state,next_check);
        ''')
        db.commit()

    def recover(self):
        self.db.execute("UPDATE target_watches SET state='uncertain',last_result='通知时服务中断，请核查飞书' "
                        "WHERE binding=? AND state='sending'", (self.binding,))
        self.db.commit()

    def validate(self, proposal):
        if not isinstance(proposal, dict) or set(proposal) != {
                'url', 'event', 'interval_minutes', 'mode'}:
            raise ValueError('监控任务格式无效。')
        url, event, minutes, mode = (proposal[key] for key in
                                     ('url', 'event', 'interval_minutes', 'mode'))
        if event != 'new_comment' or mode not in ('once', 'continuous'):
            raise ValueError('目前只支持 Issue/MR 新评论监控。')
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError('检查间隔须为 1 到 1440 分钟。')
        kind, iid, endpoint = target_endpoint(url, self.host)
        return url, kind, iid, endpoint, event, minutes, mode

    async def api(self, endpoint, paginate=False):
        args = [self.cfg['GLAB_BIN'], 'api', '--hostname', self.host, endpoint]
        if paginate:
            args += ['--paginate', '--output', 'ndjson']
        raw = await self.run(args, 60)
        if not paginate:
            return json.loads(raw)
        rows = []
        for line in raw.splitlines():
            if line.strip():
                page = json.loads(line)
                if isinstance(page, dict):
                    rows.append(page)
                elif isinstance(page, list):
                    rows.extend(page)
                else:
                    raise ValueError('GitLab 分页结果格式无效。')
        return rows

    async def snapshot(self, url, kind, iid, endpoint, request_started):
        target = await self.api(endpoint)
        if (not isinstance(target, dict) or target.get('iid') != iid
                or target.get('web_url') != url):
            raise ValueError('GitLab 目标与链接不匹配。')
        notes = flatten_notes(await self.api(endpoint + '/discussions?per_page=100', True))
        try:
            return max((note['id'] for note in notes if
                        datetime.fromisoformat(note['created_at'].replace('Z', '+00:00')).timestamp()
                        < request_started), default=0)
        except (KeyError, AttributeError, ValueError, TypeError):
            raise ValueError('GitLab 评论时间格式无效。') from None

    async def create_many(self, proposals, source, session_id):
        if not isinstance(proposals, list) or not 1 <= len(proposals) <= 20:
            raise ValueError('监控任务数量须为 1 到 20 个。')
        specs = [self.validate(item) for item in proposals]
        request = self.db.execute('SELECT started FROM task_runs WHERE source=? AND binding=?',
                                  (source, self.binding)).fetchone()
        request_started = request[0] if request else time.time()
        staged = []
        staged_urls = {}
        for index, (url, kind, iid, endpoint, event, minutes, mode) in enumerate(specs):
            item_source = source if index == 0 else f'{source}:{index + 1}'
            existing = self.db.execute('SELECT id FROM target_watches WHERE source=? AND binding=?',
                                       (item_source, self.binding)).fetchone()
            if existing:
                staged.append((existing[0], None))
                continue
            active = self.db.execute("SELECT id,interval_minutes,mode FROM target_watches WHERE binding=? AND url=? "
                                     "AND event=? AND state='active'", (self.binding, url, event)).fetchone()
            if active:
                if active[1:] != (minutes, mode):
                    raise ValueError(f'该目标已有不同配置的监控 {active[0]}，请先取消或查看原任务。')
                staged.append((active[0], None))
                continue
            if (url, event) in staged_urls:
                previous_key, previous_minutes, previous_mode = staged_urls[(url, event)]
                if (minutes, mode) != (previous_minutes, previous_mode):
                    raise ValueError('同一目标不能在一次请求中使用不同的监控配置。')
                staged.append((previous_key, None))
                continue
            baseline = await self.snapshot(url, kind, iid, endpoint, request_started)
            key = hashlib.sha256(f'{self.binding}:{item_source}:watch'.encode()).hexdigest()[:10]
            staged.append((key, (item_source, url, kind, event, minutes, mode, baseline)))
            staged_urls[(url, event)] = (key, minutes, mode)
        now = time.time()
        with self.db:
            for key, row in staged:
                if row:
                    item_source, url, kind, event, minutes, mode, baseline = row
                    self.db.execute('INSERT INTO target_watches '
                                    '(id,binding,session_id,source,url,kind,event,interval_minutes,mode,'
                                    'baseline_id,next_check,state,created,updated) '
                                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                    (key, self.binding, session_id, item_source, url, kind, event,
                                     minutes, mode, baseline, now + minutes * 60, 'active', now, now))
        return [(key, row is not None) for key, row in staged]

    def list_lines(self, session_id=None):
        rows = self.db.execute("SELECT id,url,kind,mode,interval_minutes,checked_at,last_result "
                               "FROM target_watches WHERE binding=? AND (? IS NULL OR session_id=?) "
                               "AND state='active' ORDER BY next_check LIMIT 20",
                               (self.binding, session_id, session_id)).fetchall()
        return [f'{key}｜GitLab {"Issue" if kind == "issues" else "MR"} 新评论｜'
                f'{minutes} 分钟｜{"首次通知后结束" if mode == "once" else "持续监控"}｜{url}'
                + (f'｜{result}' if result else '')
                for key, url, kind, mode, minutes, checked, result in rows]

    def detail(self, key, session_id=None):
        row = self.db.execute('SELECT url,kind,event,mode,interval_minutes,state,baseline_id,'
                              'next_check,checked_at,notified_at,last_result FROM target_watches '
                              'WHERE id=? AND binding=? AND (? IS NULL OR session_id=?)',
                              (key, self.binding, session_id, session_id)).fetchone()
        if not row:
            return None
        url, kind, event, mode, minutes, state, baseline, next_check, checked, notified, result = row
        fmt = lambda stamp: datetime.fromtimestamp(stamp, get_timezone(self.cfg)).strftime('%Y-%m-%d %H:%M') if stamp else '尚无'
        return (f'{key}｜GitLab {"Issue" if kind == "issues" else "MR"} 新评论｜{state}\n'
                f'目标：{url}\n间隔：{minutes} 分钟；模式：{"首次通知后结束" if mode == "once" else "持续监控"}'
                f'\n最近检查：{fmt(checked)}；下次检查：{fmt(next_check) if state == "active" else "无"}'
                f'\n最近通知：{fmt(notified)}；结果：{result or "尚无"}')

    def cancel(self, key, session_id=None):
        row = self.db.execute('SELECT session_id FROM target_watches WHERE id=? AND binding=?',
                              (key, self.binding)).fetchone()
        changed = self.db.execute("UPDATE target_watches SET state='cancelled',updated=? WHERE id=? "
                                  "AND binding=? AND (? IS NULL OR session_id=?) AND state='active'",
                                  (time.time(), key, self.binding, session_id, session_id)).rowcount
        self.db.commit()
        return bool(changed), row[0] if changed and row else None

    async def tick(self, now=None):
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT w.id,w.session_id,w.url,w.baseline_id,w.interval_minutes,w.mode "
            "FROM target_watches w JOIN inbox i ON i.id=w.source AND i.binding=w.binding "
            "AND i.state='done' WHERE w.binding=? AND w.state='active' AND w.next_check<=? "
            "ORDER BY w.next_check LIMIT 10", (self.binding, now)).fetchall()
        for key, sid, url, baseline, minutes, mode in rows:
            thread = self.db.execute('SELECT root_id FROM session_threads WHERE binding=? AND session_id=?',
                                     (self.binding, sid)).fetchone() if sid else None
            if sid and not thread:
                continue
            try:
                _, _, endpoint = target_endpoint(url, self.host)
                notes = flatten_notes(await self.api(endpoint + '/discussions?per_page=100', True))
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError) as error:
                logging.warning('GitLab watch query failed for %s (%s)', key, type(error).__name__)
                self.db.execute('UPDATE target_watches SET next_check=?,checked_at=?,updated=?,last_result=? '
                                "WHERE id=? AND state='active'",
                                (now + minutes * 60, now, time.time(), 'GitLab 查询失败，稍后重试', key))
                self.db.commit()
                continue
            newest = max((note['id'] for note in notes), default=baseline)
            found = sorted((note for note in notes if note['id'] > baseline and not note['system']),
                           key=lambda note: note['id'])
            if not found:
                self.db.execute('UPDATE target_watches SET baseline_id=?,next_check=?,checked_at=?,updated=?,'
                                "last_result='' WHERE id=? AND state='active'",
                                (max(baseline, newest), now + minutes * 60, now, time.time(), key))
                self.db.commit()
                continue
            label = 'Issue' if '/-/issues/' in url else 'MR'
            authors = []
            for note in found:
                author = note.get('author')
                name = author.get('name') if isinstance(author, dict) else None
                if isinstance(name, str) and name.strip():
                    clean = re.sub(r'[\r\n<>\[\]@]', ' ', name).strip()[:60]
                    if clean and clean not in authors:
                        authors.append(clean)
            content = (f'📌 监控到 GitLab {label} 新回复：{len(found)} 条\n{url}'
                       + (f'\n回复人：{"、".join(authors[:5])}' if authors else ''))
            sendkey = hashlib.sha256(f'watch:{key}:{baseline}:{newest}'.encode()).hexdigest()[:40]
            args = [self.cfg['LARK_CLI_BIN'], 'im']
            if thread:
                args += ['+messages-reply', '--as', 'bot', '--message-id', thread[0], '--reply-in-thread']
            else:
                args += ['+messages-send', '--as', 'bot', '--chat-id', self.binding]
            with self.db:
                changed = self.db.execute("UPDATE target_watches SET state='sending',checked_at=?,updated=? "
                                          "WHERE id=? AND state='active'", (now, time.time(), key)).rowcount
            if not changed:
                continue
            try:
                result = json.loads(await self.run(args + ['--markdown', content,
                    '--idempotency-key', sendkey], 30))
                from assistant.service import sent_message_id
                remote_id = sent_message_id(result)
                if result.get('ok') is not True or not remote_id:
                    raise RuntimeError('飞书未确认发送')
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                self.db.execute("UPDATE target_watches SET state='uncertain',updated=?,last_result=? WHERE id=?",
                                (time.time(), '通知结果不确定，请核查飞书', key))
                self.db.commit()
                continue
            with self.db:
                self.db.execute('UPDATE target_watches SET state=?,baseline_id=?,next_check=?,'
                                'notified_at=?,updated=?,last_result=? WHERE id=?',
                                ('sent' if mode == 'once' else 'active', max(baseline, newest),
                                 now + minutes * 60, now, time.time(), '已通知', key))
                if sid:
                    from assistant.sessions import Sessions
                    Sessions(self.db, self.binding).map_message(remote_id, sid,
                                                               f'watch:{key}:{newest}', content, 'assistant')
                    self.db.execute('INSERT OR IGNORE INTO session_history(binding,session_id,source,role,content) '
                                    'VALUES(?,?,?,?,?)', (self.binding, sid, f'watch:{key}:{newest}',
                                                       'assistant', content))
                    if mode == 'once':
                        from assistant.schedule import finish_session_if_idle
                        finish_session_if_idle(self.db, self.binding, sid)

    async def scheduler(self):
        while True:
            await self.tick()
            await asyncio.sleep(15)
