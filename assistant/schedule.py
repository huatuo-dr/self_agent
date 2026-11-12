"""Durable, owner-authorized reminders and scheduled message delivery."""
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import re
import time
from assistant.settings import get_timezone

SHANGHAI = get_timezone()
REPEATS = {'none': 0, 'daily': 86400, 'weekly': 604800}


def response_schema():
    return {'type': 'array', 'maxItems': 20, 'items':
        {'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['message', 'issue_summary']},
            'body': {'type': 'string'}, 'run_at': {'type': 'string'},
            'repeat': {'type': 'string', 'enum': list(REPEATS)}},
         'required': ['kind', 'body', 'run_at', 'repeat'], 'additionalProperties': False}}


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError('请给出明确的执行时间。')
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('执行时间须为带时区的 ISO 时间。') from None
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError('执行时间须带时区，例如 +08:00。')
    return when.timestamp()


def session_has_pending_work(db, binding, session_id):
    if not session_id:
        return False
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_jobs'").fetchone():
        if db.execute("SELECT 1 FROM scheduled_jobs WHERE binding=? AND session_id=? "
                      "AND state IN ('pending','sending','uncertain') LIMIT 1",
                      (binding, session_id)).fetchone():
            return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='issue_notices'").fetchone():
        if db.execute("SELECT 1 FROM issue_notices WHERE session_id=? "
                      "AND state IN ('resolving','draft','armed','sending','uncertain') LIMIT 1",
                      (session_id,)).fetchone():
            return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='group_watches'").fetchone():
        if db.execute("SELECT 1 FROM group_watches WHERE binding=? AND session_id=? "
                      "AND state='active' LIMIT 1", (binding, session_id)).fetchone():
            return True
        if db.execute("SELECT 1 FROM group_watch_hits h JOIN group_watches w ON w.id=h.watch_id "
                      "WHERE w.binding=? AND w.session_id=? AND h.state='uncertain' LIMIT 1",
                      (binding, session_id)).fetchone():
            return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='target_watches'").fetchone():
        if db.execute("SELECT 1 FROM target_watches WHERE binding=? AND session_id=? "
                      "AND state IN ('active','sending','uncertain') LIMIT 1",
                      (binding, session_id)).fetchone():
            return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_tasks'").fetchone():
        if db.execute("SELECT 1 FROM pipeline_tasks WHERE binding=? AND session_id=? "
                          "AND state != 'deleted' LIMIT 1", (binding, session_id)).fetchone():
            return True
    return False


def finish_session_if_idle(db, binding, session_id):
    if not session_id or session_has_pending_work(db, binding, session_id):
        return
    db.execute("UPDATE sessions SET status='已完成',next_step='' "
               "WHERE id=? AND binding=? AND next_step IN "
               "('等待定时消息发送','等待流水线成功后自动发送','监控群消息','监控 GitLab 目标')",
               (session_id, binding))


class Schedule:
    def __init__(self, cfg, db, run):
        self.cfg, self.db, self.run = cfg, db, run
        self.binding = cfg['FEISHU_OWNER_CHAT_ID']
        db.executescript('''
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id TEXT PRIMARY KEY, binding TEXT NOT NULL, session_id INTEGER,
                source TEXT UNIQUE NOT NULL, request_source TEXT, body TEXT NOT NULL, run_at REAL NOT NULL,
                repeat TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, sent_count INTEGER NOT NULL DEFAULT 0,
                last_result TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'message',
                next_try REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_jobs(binding,state,run_at);
            CREATE TABLE IF NOT EXISTS scheduled_runs (
                job_id TEXT NOT NULL, occurrence REAL NOT NULL, state TEXT NOT NULL,
                updated REAL NOT NULL, result TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(job_id,occurrence));
        ''')
        columns = {row[1] for row in db.execute('PRAGMA table_info(scheduled_jobs)')}
        for name, kind in [('kind', "TEXT NOT NULL DEFAULT 'message'"),
                           ('next_try', 'REAL NOT NULL DEFAULT 0'),
                           ('attempts', 'INTEGER NOT NULL DEFAULT 0'),
                           ('request_source', 'TEXT')]:
            if name not in columns:
                db.execute(f'ALTER TABLE scheduled_jobs ADD COLUMN {name} {kind}')
        db.execute('UPDATE scheduled_jobs SET request_source=source WHERE request_source IS NULL')
        # A crash during an external send is ambiguous, even with an idempotency key.
        db.execute("UPDATE scheduled_jobs SET state='uncertain',last_result='发送时服务中断；请核查飞书' "
                   "WHERE binding=? AND state='sending'", (self.binding,))
        db.execute("UPDATE scheduled_runs SET state='uncertain',result='发送时服务中断；请核查飞书' "
                   "WHERE state='sending' AND job_id IN "
                   "(SELECT id FROM scheduled_jobs WHERE binding=?)", (self.binding,))
        db.commit()
        self.issues = None
        if cfg.get('ISSUE_SKILL_ENABLED', False):
            from assistant.issues import IssueAssistant
            from assistant.service import ROOT, STATE
            self.issues = IssueAssistant(cfg, db, run, ROOT, STATE)

    def create(self, proposal, source, session_id):
        return self.create_many([proposal], source, session_id)[0]

    def validate(self, proposal):
        if not isinstance(proposal, dict) or set(proposal) != {'kind', 'body', 'run_at', 'repeat'}:
            raise ValueError('定时任务格式无效。')
        body, repeat, kind = proposal['body'], proposal['repeat'], proposal['kind']
        if kind not in ('message', 'issue_summary'):
            raise ValueError('不支持此定时任务类型。')
        if not isinstance(body, str) or len(body) > 6000 or (kind == 'message' and not body.strip()):
            raise ValueError('定时消息正文无效。')
        if kind == 'issue_summary' and not self.cfg.get('ISSUE_SKILL_ENABLED', False):
            raise ValueError('Issue/MR 汇总功能尚未启用。')
        if not isinstance(repeat, str) or repeat not in REPEATS:
            raise ValueError('重复周期仅支持 none、daily、weekly。')
        run_at = parse_time(proposal['run_at'])
        if run_at <= time.time() or run_at > time.time() + 366 * 86400:
            raise ValueError('首次执行时间须在未来一年内。')
        return kind, body.strip(), run_at, repeat

    def create_many(self, proposals, source, session_id):
        if not isinstance(proposals, list) or not 1 <= len(proposals) <= 20:
            raise ValueError('定时任务数量须为 1 到 20 个。')
        validated = [self.validate(item) for item in proposals]
        results = []
        with self.db:
            for index, (kind, body, run_at, repeat) in enumerate(validated):
                item_source = source if index == 0 else f'{source}:{index + 1}'
                key = hashlib.sha256(f'{self.binding}:{item_source}:schedule'.encode()).hexdigest()[:10]
                existing = self.db.execute('SELECT id FROM scheduled_jobs WHERE source=? AND binding=?',
                                           (item_source, self.binding)).fetchone()
                if existing:
                    results.append((existing[0], False))
                    continue
                now = time.time()
                self.db.execute('INSERT INTO scheduled_jobs '
                                '(id,binding,session_id,source,request_source,body,run_at,repeat,state,created,updated,kind) '
                                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                (key, self.binding, session_id, item_source, source, body, run_at, repeat,
                                 'pending', now, now, kind))
                results.append((key, True))
        return results

    def describe(self, key, row):
        body, run_at, repeat, state, sent_count, result, kind = row
        when = datetime.fromtimestamp(run_at, get_timezone(self.cfg)).strftime('%Y-%m-%d %H:%M')
        cycle = {'none': '仅一次', 'daily': '每天', 'weekly': '每周'}[repeat]
        label = 'Issue/MR 实时汇总' if kind == 'issue_summary' else body[:60]
        return (f'{key}｜{state}｜{when}（{get_timezone(self.cfg).key}，{cycle}）｜{label}'
                + (f'｜已执行 {sent_count} 次' if sent_count else '')
                + (f'｜{result}' if result else ''))

    def finish_session_if_idle(self, session_id):
        finish_session_if_idle(self.db, self.binding, session_id)

    def command(self, argument, session_id=None):
        value = argument.strip()
        if not value:
            rows = self.db.execute(
                "SELECT id,body,run_at,repeat,state,sent_count,last_result,kind FROM scheduled_jobs "
                "WHERE binding=? AND (? IS NULL OR session_id=?) "
                "AND state IN ('pending','sending') ORDER BY run_at LIMIT 20",
                (self.binding, session_id, session_id)).fetchall()
            lines = [self.describe(row[0], row[1:]) for row in rows]
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='issue_notices'").fetchone():
                notices = self.db.execute(
                    "SELECT id,payload,state,trigger_status FROM issue_notices "
                    "WHERE substr(scope,1,length(?))=? AND (? IS NULL OR session_id=?) "
                    "AND state IN ('armed','sending') "
                    "ORDER BY rowid DESC LIMIT 20", (self.binding + ':', self.binding + ':',
                                                       session_id, session_id)).fetchall()
                for key, raw, state, trigger_status in notices:
                    try:
                        trigger = json.loads(raw).get('trigger')
                    except (ValueError, TypeError, AttributeError):
                        trigger = None
                    if trigger:
                        lines.append(f'{key}｜流水线通知｜{state}｜{trigger["url"]}'
                                     + (f'｜最近状态 {trigger_status}' if trigger_status else ''))
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='group_watches'").fetchone():
                from assistant.group_watch import GroupWatch
                lines += GroupWatch(self.cfg, self.db, self.run).list_lines(session_id)
            if self.cfg.get('ISSUE_SKILL_ENABLED', False):
                from assistant.target_watch import TargetWatch
                lines += TargetWatch(self.cfg, self.db, self.run).list_lines(session_id)
            return ('生效中的任务：\n' + '\n'.join(lines) if lines else '暂无生效中的任务。') + '\n使用 /schedule cancel 编号 取消；已结束的任务可按编号查看。'
        match = re.fullmatch(r'cancel\s+([0-9a-f]{10})', value, re.I)
        if match:
            key = match[1].lower()
            job = self.db.execute('SELECT session_id FROM scheduled_jobs WHERE id=? AND binding=?',
                                  (key, self.binding)).fetchone()
            changed = self.db.execute("UPDATE scheduled_jobs SET state='cancelled',updated=? "
                                      "WHERE id=? AND binding=? AND (? IS NULL OR session_id=?) "
                                      "AND state='pending'", (time.time(), key, self.binding,
                                                               session_id, session_id)).rowcount
            if not changed and self.db.execute("SELECT 1 FROM sqlite_master WHERE name='issue_notices'").fetchone():
                notice = self.db.execute('SELECT session_id FROM issue_notices WHERE id=? AND '
                                         'substr(scope,1,length(?))=?',
                                         (key, self.binding + ':', self.binding + ':')).fetchone()
                changed = self.db.execute(
                    "UPDATE issue_notices SET state='cancelled' WHERE id=? "
                    "AND substr(scope,1,length(?))=? "
                    "AND (? IS NULL OR session_id=?) AND state='armed'",
                    (key, self.binding + ':', self.binding + ':', session_id, session_id)).rowcount
                if changed:
                    self.finish_session_if_idle(notice[0] if notice else None)
            elif changed:
                self.finish_session_if_idle(job[0] if job else None)
            if not changed and self.db.execute("SELECT 1 FROM sqlite_master WHERE name='group_watches'").fetchone():
                from assistant.group_watch import GroupWatch
                changed, watch_sid = GroupWatch(self.cfg, self.db, self.run).cancel(key, session_id)
            if not changed and self.cfg.get('ISSUE_SKILL_ENABLED', False):
                from assistant.target_watch import TargetWatch
                changed, watch_sid = TargetWatch(self.cfg, self.db, self.run).cancel(key, session_id)
                if changed:
                    self.finish_session_if_idle(watch_sid)
            self.db.commit()
            return '已取消任务。' if changed else '未找到可取消的任务；正在发送或已结束的任务不会取消。'
        match = re.fullmatch(r'([0-9a-f]{10})', value, re.I)
        if match:
            key = match[1].lower()
            row = self.db.execute(
                'SELECT body,run_at,repeat,state,sent_count,last_result,kind FROM scheduled_jobs '
                'WHERE id=? AND binding=? AND (? IS NULL OR session_id=?)',
                (key, self.binding, session_id, session_id)).fetchone()
            if not row:
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='issue_notices'").fetchone():
                    notice = self.db.execute(
                        'SELECT payload,state,trigger_status,checked_at,sent FROM issue_notices '
                        'WHERE id=? AND substr(scope,1,length(?))=? AND (? IS NULL OR session_id=?)',
                        (key, self.binding + ':', self.binding + ':', session_id, session_id)).fetchone()
                    if notice:
                        payload = json.loads(notice[0])
                        trigger = payload.get('trigger')
                        if trigger:
                            return (f'{key}｜流水线通知｜{notice[1]}\n条件：{trigger["url"]} 成功'
                                    f'\n最近检查：{datetime.fromtimestamp(notice[3], get_timezone(self.cfg)).strftime("%Y-%m-%d %H:%M") if notice[3] else "尚未检查"}'
                                    f'\n流水线状态：{notice[2] or "未知"}\n已确认发送人数：{notice[4]}')
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='group_watches'").fetchone():
                    from assistant.group_watch import GroupWatch
                    detail = GroupWatch(self.cfg, self.db, self.run).detail(key, session_id)
                    if detail:
                        return detail
                if self.cfg.get('ISSUE_SKILL_ENABLED', False):
                    from assistant.target_watch import TargetWatch
                    detail = TargetWatch(self.cfg, self.db, self.run).detail(key, session_id)
                    if detail:
                        return detail
                return '未找到此定时任务。'
            runs = self.db.execute('SELECT occurrence,state,result FROM scheduled_runs WHERE job_id=? '
                                   'ORDER BY occurrence DESC LIMIT 10', (key,)).fetchall()
            details = [self.describe(key, row), '最近执行记录：']
            details += [f'{datetime.fromtimestamp(at, get_timezone(self.cfg)).strftime("%m-%d %H:%M")}｜{state}'
                        + (f'｜{result}' if result else '') for at, state, result in runs]
            return '\n'.join(details + ([] if runs else ['暂无。']))
        return '用法：/schedule [编号] 或 /schedule cancel 编号。创建提醒请说出时间和内容；群监控请指定群 ID 或群名。'

    async def tick(self, now=None):
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT j.id,j.session_id,j.body,j.run_at,j.repeat,j.kind,j.attempts FROM scheduled_jobs j "
            "JOIN inbox i ON i.id=j.request_source AND i.binding=j.binding AND i.state='done' "
            "WHERE j.binding=? AND j.state='pending' AND j.run_at<=? AND j.next_try<=? "
            "ORDER BY j.run_at LIMIT 10",
            (self.binding, now, now)).fetchall()
        for key, session_id, body, occurrence, repeat, kind, attempts in rows:
            # A pending inbox reply takes precedence so a newly created topic can be bound first.
            thread = self.db.execute('SELECT root_id FROM session_threads WHERE binding=? AND session_id=?',
                                     (self.binding, session_id)).fetchone() if session_id else None
            if session_id and not thread:
                continue
            content = body
            if kind == 'issue_summary':
                try:
                    items = await self.issues.fetch_all(include_authored_issues=True)
                    content = self.issues.format_items(items, '定时 Issue / MR 汇总', group_issue_roles=True)
                except (RuntimeError, ValueError, OSError, KeyError, TypeError, asyncio.TimeoutError):
                    attempts += 1
                    with self.db:
                        self.db.execute('UPDATE scheduled_jobs SET attempts=?,next_try=?,last_result=?,updated=? '
                                        'WHERE id=? AND state=\'pending\'',
                                        (attempts, now + 600, 'GitLab 查询失败，稍后重试', time.time(), key))
                        self.db.execute('INSERT INTO scheduled_runs(job_id,occurrence,state,updated,result) '
                                        'VALUES(?,?,?,?,?) ON CONFLICT(job_id,occurrence) DO UPDATE SET '
                                        'state=excluded.state,updated=excluded.updated,result=excluded.result',
                                        (key, occurrence, 'query_failed', time.time(), 'GitLab 查询失败'))
                    continue
            args = [self.cfg['LARK_CLI_BIN'], 'im']
            if thread:
                args += ['+messages-reply', '--as', 'bot', '--message-id', thread[0], '--reply-in-thread']
            else:
                args += ['+messages-send', '--as', 'bot', '--chat-id', self.binding]
            sendkey = hashlib.sha256(f'schedule:{key}:{occurrence}'.encode()).hexdigest()[:40]
            with self.db:
                changed = self.db.execute("UPDATE scheduled_jobs SET state='sending',updated=? "
                                          "WHERE id=? AND binding=? AND state='pending'", (time.time(), key, self.binding)).rowcount
                if not changed:
                    continue
                self.db.execute('INSERT OR IGNORE INTO scheduled_runs VALUES(?,?,?,?,?)',
                                (key, occurrence, 'sending', time.time(), ''))
            try:
                result = json.loads(await self.run(args + ['--markdown', content, '--idempotency-key', sendkey], 30))
                if result.get('ok') is not True:
                    raise RuntimeError('Feishu did not confirm delivery')
                from assistant.service import sent_message_id
                remote_id = sent_message_id(result)
                if not remote_id:
                    raise RuntimeError('Feishu did not return a message ID')
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                with self.db:
                    self.db.execute("UPDATE scheduled_jobs SET state='uncertain',updated=?,last_result=? WHERE id=?",
                                    (time.time(), '发送结果不确定，请核查飞书', key))
                    self.db.execute("UPDATE scheduled_runs SET state='uncertain',updated=?,result=? "
                                    "WHERE job_id=? AND occurrence=?",
                                    (time.time(), '发送结果不确定，请核查飞书', key, occurrence))
                    if session_id:
                        self.db.execute("UPDATE sessions SET status='受阻',next_step=? WHERE id=? AND binding=? "
                                        "AND next_step='等待定时消息发送'",
                                        ('核查定时消息是否送达', session_id, self.binding))
                continue
            next_at = occurrence
            if repeat != 'none':
                zone = get_timezone(self.cfg)
                local = datetime.fromtimestamp(occurrence, zone)
                # Preserve the original wall time even after a nonexistent DST time
                # was normalized forward; the durable run history is the anchor.
                first = self.db.execute('SELECT MIN(occurrence) FROM scheduled_runs WHERE job_id=?',
                                        (key,)).fetchone()[0]
                wall = datetime.fromtimestamp(first, zone).timetz()
                local = datetime.combine(local.date(), wall, zone)
                step = timedelta(days=1 if repeat == 'daily' else 7)
                while next_at <= now:
                    local += step
                    next_at = local.timestamp()
            with self.db:
                self.db.execute('UPDATE scheduled_jobs SET state=?,run_at=?,updated=?,sent_count=sent_count+1,'
                                'last_result=?,attempts=0,next_try=0 '
                                'WHERE id=?', ('sent' if repeat == 'none' else 'pending', next_at if repeat != 'none' else occurrence,
                                             time.time(), '已送达', key))
                self.db.execute("UPDATE scheduled_runs SET state='sent',updated=?,result='已送达' "
                                'WHERE job_id=? AND occurrence=?', (time.time(), key, occurrence))
                if session_id:
                    from assistant.sessions import Sessions
                    Sessions(self.db, self.binding).map_message(remote_id, session_id,
                                                               f'schedule:{key}:{occurrence}', content, 'assistant')
                    self.db.execute('INSERT OR IGNORE INTO session_history(binding,session_id,source,role,content) '
                                    'VALUES(?,?,?,?,?)', (self.binding, session_id,
                                                       f'schedule:{key}:{occurrence}', 'assistant', content))
                    if repeat == 'none':
                        self.finish_session_if_idle(session_id)

    async def scheduler(self):
        while True:
            await self.tick()
            await asyncio.sleep(15)
