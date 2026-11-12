"""GitLab watch list, local workday checks and reviewed Feishu notifications."""
import asyncio
from datetime import date, datetime, time as daytime
import hashlib
import json
import logging
from pathlib import Path
import re
from urllib.parse import quote, urlencode, urlsplit
from assistant.settings import get_timezone, local_now, sign_message, strip_bot_mention

_UNSCOPED = object()

EMAIL = re.compile(r'[^\s@,，]+@[^\s@,，]+\.[^\s@,，]+')
REF = re.compile(r'https://[A-Za-z0-9.-]+/[^\s<>"\[\]()]+/-/(?:issues|merge_requests)/[0-9]+')


def now_local(cfg=None):
    return local_now(cfg)


def parse_ref(url):
    parts = urlsplit(url)
    match = re.fullmatch(r'/(.+)/-/(issues|merge_requests)/(\d+)', parts.path)
    if parts.scheme != 'https' or not match or parts.username or parts.password or parts.port:
        raise ValueError('Invalid GitLab Issue/MR URL')
    project, kind, iid = match.groups()
    if any(part in ('', '.', '..') for part in project.split('/')):
        raise ValueError('Invalid project path')
    return parts.hostname, project, kind, int(iid)


def ref_key(url):
    host, project, kind, iid = parse_ref(url)
    return f'{host}/{project}/{kind}/{iid}'


def signed(body, cfg=None):
    return sign_message(body, cfg)


def valid_address(value):
    return (isinstance(value, str) and len(value) <= 100
            and not any(ord(c) < 32 for c in value))


def prepare_greetings(payload):
    """Freeze each recipient's greeting before review; leave legacy drafts intact."""
    if payload.get('greeting_version') != 1:
        return
    for person in payload['recipients']:
        person.pop('body', None)
        if not person.get('open_id'):
            continue
        label = person.get('address_as', '').strip()
        if not label:
            label = re.split(r'[（(]', person.get('feishu_name') or '', maxsplit=1)[0].strip()
        if not label or not valid_address(label):
            person['open_id'] = None
            person['error'] = '缺少可用的飞书显示名，请补充称呼或重新确认收件人'
            continue
        body = payload['body']
        greeting = re.match(r'^你好[，,]\s*[^\n，,。.!！?？：:；;]{1,100}(?=[\n，,。.!！?？：:；;]|$)', body)
        person['body'] = ('你好，' + label + body[greeting.end():] if greeting
                          else '你好，' + label + '\n' + body)


def notice_messages(payload):
    return [(person.get('open_id'), person.get('body', payload['body']))
            for person in payload['recipients']]


def pipeline_trigger(value, host):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'kind', 'url'} or value['kind'] != 'gitlab_pipeline':
        raise ValueError('Unsupported notification trigger')
    parts = urlsplit(value['url']) if isinstance(value['url'], str) else None
    match = re.fullmatch(r'/(.+)/-/pipelines/([1-9][0-9]*)', parts.path) if parts else None
    if (not parts or parts.scheme != 'https' or parts.hostname != host or parts.port
            or parts.username or parts.password or parts.query or parts.fragment or not match
            or any(part in ('', '.', '..') for part in match[1].split('/'))):
        raise ValueError('Invalid GitLab pipeline URL')
    return {'kind': 'gitlab_pipeline', 'url': value['url']}


def approves_pipeline_send(text):
    if not isinstance(text, str) or not re.search(r'(?:pipeline|流水线)', text, re.I):
        return False
    if not re.search(r'(?:成功|通过|完成)', text) or '发送' not in text:
        return False
    without_immediate = re.sub(r'(?:不要|暂不|先别)(?:现在|立即|马上)发送', '', text)
    if re.search(r'(?:不要|暂不|先别|不能|不可以).*发送', without_immediate):
        return False
    match = re.search(
        r'(?:消息|内容|正文).{0,12}(?:没有问题|没问题|可以|确认|同意)|(?:同意|确认).{0,12}发送',
        text)
    return bool(match and not re.match(r'\s*(?:吗|么|？|\?)', text[match.end():]))


def pipeline_endpoint(trigger):
    parts = urlsplit(trigger['url'])
    match = re.fullmatch(r'/(.+)/-/pipelines/([1-9][0-9]*)', parts.path)
    return f'projects/{quote(match[1], safe="")}/pipelines/{match[2]}', int(match[2])


def validate_calendar(payload, year):
    """Validate explicit local annual exceptions; empty exception lists are valid."""
    if not isinstance(payload, dict) or type(payload.get('year')) is not int or payload['year'] != year:
        raise ValueError('Wrong calendar year / 日历年份不符')
    for field in ('holidays', 'workdays'):
        values = payload.get(field)
        if not isinstance(values, list):
            raise ValueError(f'Calendar {field} must be a list')
        for value in values:
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('Calendar dates must use YYYY-MM-DD')
            parsed = date.fromisoformat(value)
            if parsed.year != year:
                raise ValueError('Date outside calendar year')
    if set(payload['holidays']) & set(payload['workdays']):
        raise ValueError('Conflicting calendar dates')
    return payload


def is_workday(day, calendar=None):
    calendar = calendar or {}
    if calendar.get('mode') == 'everyday':
        return True
    if day.isoformat() in calendar.get('workdays', []):
        return True
    return day.weekday() < 5 and day.isoformat() not in calendar.get('holidays', [])


def validate_issue_config(cfg):
    """Validate a same-day watch window without opening any calendar file."""
    clocks = []
    for key, default in [('ISSUE_WATCH_START_TIME', '09:00'), ('ISSUE_SUMMARY_TIME', '09:00'),
                         ('ISSUE_WATCH_END_TIME', '21:00')]:
        value = cfg.get(key, default)
        if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
            raise ValueError(f'{key} 必须为 HH:MM（00:00 至 23:59）')
        clocks.append(daytime.fromisoformat(value))
    if not clocks[0] <= clocks[1] <= clocks[2]:
        raise ValueError('Issue 巡检不支持跨夜，须满足 ISSUE_WATCH_START_TIME <= ISSUE_SUMMARY_TIME <= ISSUE_WATCH_END_TIME')
    interval = cfg.get('ISSUE_WATCH_INTERVAL_SECONDS', 600)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 < interval < float('inf'):
        raise ValueError('ISSUE_WATCH_INTERVAL_SECONDS 必须为正数')
    mode = cfg.get('WORKDAY_MODE', 'weekdays')
    if mode not in ('weekdays', 'everyday', 'calendar_file'):
        raise ValueError('WORKDAY_MODE 仅支持 weekdays、everyday、calendar_file')
    if mode == 'calendar_file' and (not isinstance(cfg.get('WORKDAY_CALENDAR_FILE'), str)
                                    or not cfg['WORKDAY_CALENDAR_FILE'].strip()):
        raise ValueError('calendar_file 模式须设置 WORKDAY_CALENDAR_FILE')
    return *clocks, interval


class IssueAssistant:
    def __init__(self, cfg, db, run, root, state):
        validate_issue_config(cfg)
        self.cfg, self.db, self.run = cfg, db, run
        self.root, self.state = Path(root), Path(state)
        self.host = cfg['GITLAB_HOST']
        self.binding = cfg['FEISHU_OWNER_CHAT_ID']
        self.scope = self.binding + ':' + self.host
        db.executescript('''
            CREATE TABLE IF NOT EXISTS issue_pool (
                scope TEXT, ref TEXT, reason TEXT, seen_at TEXT, PRIMARY KEY(scope,ref));
            CREATE TABLE IF NOT EXISTS issue_days (
                scope TEXT, day TEXT, calendar TEXT, workday INTEGER, reset INTEGER DEFAULT 0,
                last_slot INTEGER DEFAULT -1, PRIMARY KEY(scope,day));
            CREATE TABLE IF NOT EXISTS issue_calendar (year INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE IF NOT EXISTS issue_outbox (
                id TEXT PRIMARY KEY, scope TEXT, day TEXT, content TEXT, refs TEXT,
                sent INTEGER DEFAULT 0, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS issue_notices (
                id TEXT PRIMARY KEY, scope TEXT, source TEXT UNIQUE, payload TEXT,
                reviewed INTEGER DEFAULT 0, state TEXT DEFAULT 'draft', sent INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS issue_contacts (
                scope TEXT, gitlab_id INTEGER, email TEXT, PRIMARY KEY(scope,gitlab_id));
        ''')
        if 'session_id' not in {r[1] for r in db.execute('PRAGMA table_info(issue_notices)')}:
            db.execute('ALTER TABLE issue_notices ADD COLUMN session_id INTEGER')
        columns = {r[1] for r in db.execute('PRAGMA table_info(issue_notices)')}
        for name, kind in [('checked_at', 'REAL'), ('trigger_status', 'TEXT'),
                           ('report_state', "TEXT DEFAULT ''"), ('report_text', "TEXT DEFAULT ''")]:
            if name not in columns:
                db.execute(f'ALTER TABLE issue_notices ADD COLUMN {name} {kind}')
        db.execute('UPDATE issue_notices SET session_id=(SELECT session_id FROM inbox WHERE inbox.id=issue_notices.source) WHERE session_id IS NULL')
        db.execute("UPDATE issue_notices SET state='uncertain' WHERE scope=? AND state='sending'", (self.scope,))
        db.execute("UPDATE issue_notices SET report_state='uncertain' "
                   "WHERE scope=? AND report_state='sending'", (self.scope,))
        for key, raw, report_state in db.execute(
                "SELECT id,payload,report_state FROM issue_notices WHERE scope=? AND state='uncertain'",
                (self.scope,)).fetchall():
            try:
                deferred = bool(json.loads(raw).get('trigger'))
            except (ValueError, TypeError, AttributeError):
                deferred = False
            if deferred and not report_state:
                db.execute("UPDATE issue_notices SET report_state='pending',report_text=? WHERE id=?",
                           (f'定时通知 {key}：发送时服务中断，结果不确定，请先核查飞书。', key))
        for key, raw in db.execute("SELECT id,payload FROM issue_notices WHERE scope=? AND state='draft'",
                                   (self.scope,)).fetchall():
            try:
                recipients = json.loads(raw).get('recipients', [])
            except (ValueError, TypeError, AttributeError):
                recipients = []
            if not recipients or not all(isinstance(p, dict) and p.get('open_id') for p in recipients):
                db.execute("UPDATE issue_notices SET state='resolving',reviewed=0 WHERE id=?", (key,))
        db.commit()

    async def api(self, endpoint, paginate=False):
        args = [self.cfg['GLAB_BIN'], 'api', '--hostname', self.host, endpoint]
        if paginate:
            args += ['--paginate', '--output', 'ndjson']
        output = await self.run(args, 60)
        if paginate:
            result = []
            for line in output.splitlines():
                if line.strip():
                    value = json.loads(line)
                    result.extend(value if isinstance(value, list) else [value])
            if not all(isinstance(item, dict) for item in result):
                raise ValueError('Unexpected GitLab list response')
            return result
        return json.loads(output)

    async def fetch_all(self, *, include_authored_issues=False):
        user = await self.api('user')
        uid = int(user['id'])
        specs = [('issues', 'assignee_id', '指派给我'),
                 ('merge_requests', 'reviewer_id', '需要我审批'),
                 ('merge_requests', 'author_id', '我创建的')]
        if include_authored_issues:
            specs.insert(1, ('issues', 'author_id', '我创建的'))
        # Fetch every page of every category; a failed category aborts the whole snapshot.
        lists = await asyncio.gather(*[
            self.api(kind + '?' + urlencode({'scope': 'all', 'state': 'opened', field: uid,
                                            'per_page': 100}), True)
            for kind, field, _ in specs])
        items = {}
        for (kind, _, reason), rows in zip(specs, lists):
            for row in rows:
                if row.get('state') != 'opened':
                    continue
                url = row['web_url']
                if parse_ref(url)[0] != self.host:
                    raise ValueError('GitLab response host mismatch')
                key = ref_key(url)
                entry = items.setdefault(key, {'url': url, 'title': row['title'], 'kind': kind,
                                              'iid': row['iid'], 'reasons': [],
                                              'author': row.get('author'),
                                              'assignees': row.get('assignees') or []})
                if reason not in entry['reasons']:
                    entry['reasons'].append(reason)
        return list(items.values())

    def remember(self, urls, reason):
        for url in urls:
            if parse_ref(url)[0] == self.host:
                self.db.execute('INSERT OR REPLACE INTO issue_pool VALUES(?,?,?,?)',
                                (self.scope, ref_key(url), reason, now_local(self.cfg).isoformat()))
        self.db.commit()

    def remember_text(self, text, reason):
        for url in REF.findall(text):
            try:
                self.remember([url], reason)
            except ValueError:
                continue

    def unseen(self, items):
        known = {r[0] for r in self.db.execute('SELECT ref FROM issue_pool WHERE scope=?', (self.scope,))}
        return [item for item in items if ref_key(item['url']) not in known]

    @staticmethod
    def format_items(items, title, *, group_issue_roles=False):
        def person_name(person):
            person = person or {}
            name = person.get('name') or person.get('username') or '未知用户'
            return re.sub(r'[\[\]<>\r\n]', ' ', name)

        lines = [title, '']
        issues = [item for item in items if item['kind'] == 'issues']
        if group_issue_roles:
            groups = [('我创建的 Issue', [item for item in issues if '我创建的' in item['reasons']]),
                      ('指派给我的 Issue', [item for item in issues if '我创建的' not in item['reasons']])]
            mrs = [item for item in items if item['kind'] == 'merge_requests']
            groups += [('我创建的 MR', [item for item in mrs if '我创建的' in item['reasons']]),
                       ('需要我审批的 MR', [item for item in mrs
                                    if '需要我审批' in item['reasons'] and '我创建的' not in item['reasons']])]
        else:
            groups = [('Issue', issues),
                      ('MR', [item for item in items if item['kind'] == 'merge_requests'])]
        number = 0
        for label, subset in groups:
            lines += [f'**{label}：{len(subset)} 条**', '']
            for item in subset:
                # Treat remote titles as data; avoid Markdown mentions/images.
                title = re.sub(r'[\[\]<>\r\n]', ' ', item['title'])
                number += 1
                prefix = f'{number}.' if group_issue_roles else '-'
                if group_issue_roles:
                    if label == '我创建的 Issue':
                        names = [person_name(person) for person in item.get('assignees') or []]
                        detail = '指派给：' + '、'.join(names) if names else '未指派'
                    else:
                        detail = '创建者：' + person_name(item.get('author'))
                    suffix = f'（{detail}）'
                elif item['kind'] == 'issues':
                    author = person_name(item.get('author'))
                    suffix = f'【{author}创建】' if author != '未知用户' else '【创建者未知】'
                else:
                    detail = '、'.join(item['reasons'])
                    suffix = f'（{detail}）'
                lines.append(f"{prefix} [{title}]({item['url']}){suffix}")
            lines.append('')
        return '\n'.join(lines).strip()

    async def calendar(self, day):
        """Read only the selected local calendar mode; never invoke an agent or network."""
        validate_issue_config(self.cfg)
        mode = self.cfg.get('WORKDAY_MODE', 'weekdays')
        if mode != 'calendar_file':
            return {'year': day.year, 'holidays': [], 'workdays': [], 'mode': mode}
        path = Path(self.cfg['WORKDAY_CALENDAR_FILE']).expanduser()
        if not path.is_absolute():
            path = self.root / path
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            result = validate_calendar(payload, day.year)
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f'WORKDAY_CALENDAR_FILE 无法读取有效的 {day.year} 年日历：{error}') from error
        return {**result, 'mode': mode}

    async def send_bot(self, content, key):
        payload = json.loads(await self.run([
            self.cfg['LARK_CLI_BIN'], 'im', '+messages-send', '--as', 'bot',
            '--chat-id', self.binding, '--markdown', content, '--idempotency-key', key], 30))
        if payload.get('ok') is not True:
            raise RuntimeError('Bot send not confirmed')

    async def full_refresh(self, *, source=None, day=None, slot=None):
        """Shared daily/manual snapshot: query fully before replacing pool and outbox."""
        lock = self.cfg.setdefault('_issue_refresh_lock', asyncio.Lock())
        async with lock:
            items = await self.fetch_all(include_authored_issues=True)
            if source is None and self.manual_pending():
                return None
            content = self.format_items(items, '当前全部 Issue / MR 待办', group_issue_roles=True)
            with self.db:
                self.db.execute('DELETE FROM issue_pool WHERE scope=?', (self.scope,))
                self.db.execute('UPDATE issue_outbox SET done=1 WHERE scope=? AND done=0', (self.scope,))
                if source is not None:
                    self.db.execute('UPDATE inbox SET issue_refs=? WHERE id=? AND binding=?',
                                    (json.dumps([item['url'] for item in items]), source, self.binding))
                else:
                    self.queue(day, 'morning', items, content=content)
                    self.db.execute('UPDATE issue_days SET reset=1,last_slot=? WHERE scope=? AND day=?',
                                    (slot, self.scope, day))
            return content

    def queue(self, day, slot, items, title=None, *, content=None):
        key = hashlib.sha256(f'{self.scope}:{day}:{slot}'.encode()).hexdigest()[:40]
        self.db.execute('INSERT OR IGNORE INTO issue_outbox(id,scope,day,content,refs) VALUES(?,?,?,?,?)',
                        (key, self.scope, day, content if content is not None else self.format_items(items, title),
                         json.dumps([item['url'] for item in items])))

    async def flush(self, day):
        rows = self.db.execute('SELECT id,content,refs,sent FROM issue_outbox '
                               'WHERE scope=? AND day=? AND done=0 ORDER BY rowid', (self.scope, day)).fetchall()
        for key, content, refs, sent in rows:
            chunks = [content[i:i+2000] for i in range(0, len(content), 2000)]
            for index in range(sent, len(chunks)):
                if self.db.execute('SELECT done FROM issue_outbox WHERE id=?', (key,)).fetchone()[0]:
                    break
                subkey = hashlib.sha256(f'{key}:{index}'.encode()).hexdigest()[:40]
                await self.send_bot(chunks[index], subkey)
                self.db.execute('UPDATE issue_outbox SET sent=? WHERE id=?', (index + 1, key))
                self.db.commit()
            if self.db.execute('SELECT done FROM issue_outbox WHERE id=?', (key,)).fetchone()[0]:
                continue
            self.remember(json.loads(refs), 'notified')
            self.db.execute('UPDATE issue_outbox SET done=1 WHERE id=?', (key,))
            self.db.commit()

    async def tick(self, current):
        start, summary, end, interval = validate_issue_config(self.cfg)
        current = current.astimezone(get_timezone(self.cfg))
        clock = current.time().replace(tzinfo=None)
        if clock < start or clock > end or self.manual_pending():
            return
        day = current.date().isoformat()
        zone = get_timezone(self.cfg)
        start_at = datetime.combine(current.date(), start, zone).timestamp()
        row = self.db.execute('SELECT workday,reset,last_slot,calendar FROM issue_days WHERE scope=? AND day=?',
                              (self.scope, day)).fetchone()
        # Keep the coordinate system alongside the day, so a reload can translate
        # its checkpoint without replaying the completed daily summary.
        coordinates = {'start': start.isoformat(timespec='minutes'),
                       'interval': interval, 'timezone': zone.key}
        previous = {'start': '09:00', 'interval': 600, 'timezone': 'Asia/Shanghai'}
        metadata = json.loads(row[3] or '{}').get('_watch', {}) if row else {}
        previous.update({key: metadata[key] for key in previous if key in metadata})
        last_slot = row[2] if row else -1
        checked_at = metadata.get('checked_at')
        if row and last_slot >= 0 and checked_at is None:
            # Legacy checkpoints used the default coordinates, without an exact
            # timestamp. Their slot boundary is the compatible lower bound.
            old_start = datetime.combine(current.date(), daytime.fromisoformat(previous['start']),
                                         get_timezone({'TIMEZONE': previous['timezone']})).timestamp()
            checked_at = old_start + last_slot * previous['interval']
        if previous != coordinates:
            last_slot = int((checked_at - start_at) // interval) if checked_at is not None else -1
        # Re-read the selected local calendar every tick while retaining watch
        # metadata; disabled calendar-file modes never open the file.
        calendar = {**await self.calendar(current.date()),
                    '_watch': {**coordinates, 'checked_at': checked_at}}
        workday = int(is_workday(current.date(), calendar))
        self.db.execute('INSERT INTO issue_days(scope,day,calendar,workday,last_slot) VALUES(?,?,?,?,?) '
                        'ON CONFLICT(scope,day) DO UPDATE SET '
                        'calendar=excluded.calendar,workday=excluded.workday,last_slot=excluded.last_slot',
                        (self.scope, day, json.dumps(calendar, ensure_ascii=False), workday, last_slot))
        self.db.commit()
        row = (workday, row[1], last_slot) if row else (workday, 0, -1)
        if not row[0]:
            return
        # Absolute elapsed time prevents repeated slots when a DST clock rolls back.
        slot = int((current.timestamp() - start_at) // interval)
        await self.flush(day)
        summary_due = not row[1] and clock >= summary
        if not summary_due and slot <= row[2]:
            return
        if summary_due:
            if await self.full_refresh(day=day, slot=slot) is None:
                return
        else:
            items = await self.fetch_all()
            if self.manual_pending():
                return
            pending = self.unseen(items)
            if pending:
                # Different coordinates may reuse the same numeric slot. Include
                # them in outbox identity so an old delivered slot cannot hide a new hit.
                label = f'{zone.key}:{coordinates["start"]}:{interval}:{slot}'
                self.queue(day, label, pending, '新增 Issue / MR 待办')
        calendar['_watch']['checked_at'] = current.timestamp()
        self.db.execute('UPDATE issue_days SET last_slot=?,calendar=? WHERE scope=? AND day=?',
                        (slot, json.dumps(calendar, ensure_ascii=False), self.scope, day))
        self.db.commit()
        await self.flush(day)

    def manual_pending(self):
        # A manual refresh has cleared the pool but its full reply is still being sent.
        return self.db.execute("SELECT 1 FROM inbox WHERE binding=? AND state='pending' "
                               "AND issue_refs IS NOT NULL LIMIT 1", (self.binding,)).fetchone() is not None

    async def scheduler(self):
        warned_day = None
        while True:
            delay = 30
            try:
                delay = min(delay, validate_issue_config(self.cfg)[3])
                await self.tick(now_local(self.cfg))
            except (RuntimeError, ValueError, OSError, KeyError, TypeError, asyncio.TimeoutError) as error:
                delay = 600
                logging.warning('Issue scheduler failed (%s)', type(error).__name__)
                from assistant.management import record_error
                record_error(self.db, 'Issue 巡检失败：' + type(error).__name__)
                today = now_local(self.cfg).date().isoformat()
                if warned_day != today:
                    try:
                        await self.send_bot('Issue 巡检暂未完成：工作日核验、GitLab 查询或发送失败，'
                                            '将自动重试；可发送 /issue 手动查询。',
                                            hashlib.sha256(f'{self.scope}:error:{today}'.encode()).hexdigest()[:40])
                        warned_day = today
                    except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                        pass
            await asyncio.sleep(delay)

    def delivered(self, message_id):
        refs = self.db.execute('SELECT issue_refs FROM inbox WHERE id=?', (message_id,)).fetchone()
        if refs and refs[0]:
            self.remember(json.loads(refs[0]), 'manual')
        self.db.execute('UPDATE issue_notices SET reviewed=1 WHERE scope=? AND source=?',
                        (self.scope, message_id))
        self.db.commit()

    async def search_contact(self, query):
        payload = json.loads(await self.run([
            self.cfg['LARK_CLI_BIN'], 'contact', '+search-user', '--as', 'user',
            '--query', query, '--exclude-external-users', '--format', 'json'], 20))
        if payload.get('ok') is not True:
            raise RuntimeError('Contact lookup not confirmed')
        data = payload.get('data') or {}
        users = data.get('users') or []
        if not isinstance(users, list):
            raise ValueError('Invalid contact lookup response')
        return [p for p in users if isinstance(p, dict) and not p.get('is_cross_tenant')
                and isinstance(p.get('open_id'), str)
                and re.fullmatch(r'ou_[A-Za-z0-9_]+', p['open_id'])], bool(data.get('has_more'))

    @staticmethod
    def contact_candidate(person):
        return {'open_id': person['open_id'], 'name': person.get('localized_name') or '',
                'email': person.get('enterprise_email') or person.get('email') or '',
                'department': person.get('department') or '',
                'chat_recency_hint': person.get('chat_recency_hint') or ''}

    async def resolve(self, person):
        gitlab_id = person.get('gitlab_id')
        email = str(person.get('email') or '').strip()
        if not EMAIL.fullmatch(email):
            email = ''
        gitlab_user = None
        if not email and isinstance(gitlab_id, int):
            cached = self.db.execute('SELECT email FROM issue_contacts WHERE scope=? AND gitlab_id=?',
                                    (self.scope, gitlab_id)).fetchone()
            if cached:
                email = cached[0]
            else:
                user = await self.api(f'users/{gitlab_id}')
                gitlab_user = user
                email = user.get('email') or user.get('public_email') or ''
        result = {**person, 'email': email, 'open_id': None}
        try:
            candidate_id = None
            if not EMAIL.fullmatch(email):
                name = ((gitlab_user or {}).get('name') if isinstance(gitlab_id, int)
                        else person.get('name')) or ''
                name = str(name).strip()
                if not name:
                    result['error'] = '缺少可检索的收件人姓名，请补充邮箱'
                    return result
                found, has_more = await self.search_contact(name)
                exact = [p for p in found if str(p.get('localized_name') or '').strip() == name]
                candidates = exact or found
                if has_more or len(candidates) != 1 or not exact:
                    result['error'] = ('飞书搜索结果未完整，请提供更精确姓名或邮箱' if has_more
                                       else '飞书姓名未唯一匹配，请选择收件人或补充邮箱')
                    result['candidates'] = [self.contact_candidate(p) for p in candidates[:20]]
                    return result
                candidate = candidates[0]
                if not isinstance(gitlab_id, int):
                    result.update(open_id=candidate['open_id'],
                                  email=candidate.get('enterprise_email') or candidate.get('email') or '',
                                  feishu_name=candidate.get('localized_name') or name,
                                  department=candidate.get('department') or '',
                                  match_source='飞书姓名精确匹配；请核对收件人')
                    return result
                email = candidate.get('enterprise_email') or candidate.get('email') or ''
                if not EMAIL.fullmatch(email):
                    result['error'] = '飞书同名用户邮箱不可见，请补充邮箱'
                    result['candidates'] = [self.contact_candidate(candidate)]
                    return result
                candidate_id = candidate.get('open_id')
                result.update(email=email, match_source='飞书同名候选，邮箱已复查；请核对收件人',
                              department=candidate.get('department', ''), name=name)
            found, has_more = await self.search_contact(email)
            matches = [p for p in found if email.casefold() in {
                str(p.get('email') or '').casefold(), str(p.get('enterprise_email') or '').casefold()}]
            if has_more or len(matches) != 1:
                result['error'] = '邮箱未唯一匹配飞书用户，请核对邮箱'
                result['candidates'] = [self.contact_candidate(p) for p in matches[:20]]
            elif candidate_id and matches[0].get('open_id') != candidate_id:
                result['error'] = '姓名与邮箱查询结果不一致，请补充正确邮箱'
            else:
                result['open_id'] = matches[0]['open_id']
                result['feishu_name'] = matches[0].get('localized_name') or person.get('name') or ''
                result['department'] = matches[0].get('department') or ''
        except (RuntimeError, ValueError, OSError, KeyError, TypeError, asyncio.TimeoutError):
            result['error'] = '飞书通讯录查询失败，请检查用户授权与权限'
        return result

    def recipient_options(self, key, payload):
        lines = ['收件人尚未确认，消息未发送，也没有可同意发送的草稿。', '请先确认以下对象：']
        for index, person in enumerate(payload['recipients'], 1):
            if person.get('open_id'):
                continue
            lines.append(f"{index}. {person.get('name') or '姓名未知'}：{person.get('error') or '待确认'}")
            for choice, candidate in enumerate(person.get('candidates', []), 1):
                lines.append(f"   候选 {choice}：{candidate.get('name') or '姓名未知'} / "
                             f"{candidate.get('email') or '邮箱未公开'} / "
                             f"{candidate.get('department') or '部门未知'}")
            if person.get('candidates'):
                lines.append(f'   选择请回复：选择收件人 {key} {index} 候选序号')
            lines.append(f'   也可回复：邮箱 {key} {index} 同事邮箱')
        lines.append(f'取消此次请求请回复：取消通知 {key}')
        return '\n'.join(lines)

    def review(self, key, payload):
        if not all(p.get('open_id') for p in payload['recipients']):
            return self.recipient_options(key, payload)
        lines = ['拟发送飞书通知，请审阅：', '', '发送身份：你本人', '', '收件人：']
        for index, p in enumerate(payload['recipients'], 1):
            lines.append(f"{index}. {p.get('feishu_name') or p.get('name') or '待确认用户'}："
                         f"{p.get('email') or '邮箱未知'}"
                         + (f"（{p['error']}）" if p.get('error') else f"（{p['open_id']}）"))
            if p.get('match_source'):
                lines.append(f"   {p['match_source']}；部门：{p.get('department') or '未知'}")
        messages = notice_messages(payload)
        if len({body for _, body in messages}) == 1:
            lines += ['', '拟发送全文：', '', messages[0][1], '']
        else:
            for index, (_, body) in enumerate(messages, 1):
                lines += ['', f'发给收件人 {index} 的拟发送全文：', '', body, '']
        if payload.get('trigger'):
            lines += [f'发送条件：{payload["trigger"]["url"]} 的 pipeline 成功。',
                      '确认后将等待条件满足，并自动发送上述原文。']
        lines += ['同意发送请回复：同意、OK 或 好的。', f'取消请回复：取消通知 {key}']
        return '\n'.join(lines)

    async def process_answer(self, result, source):
        """Model may propose a draft, never authorize a send."""
        try:
            data = json.loads(result)
        except ValueError:
            self.remember_text(result, 'discussed')
            return result
        if not isinstance(data, dict) or not isinstance(data.get('reply'), str):
            return result
        reply = data['reply']
        self.remember_text(reply, 'discussed')
        for ref in data.get('issue_refs', []):
            if isinstance(ref, str):
                self.remember_text(ref, 'discussed')
        proposal = data.get('notification')
        if not proposal:
            row = self.db.execute('SELECT session_id FROM inbox WHERE id=? AND binding=?',
                                  (source, self.binding)).fetchone()
            if row and row[0] is not None:
                pending = self.db.execute("SELECT id,state,payload FROM issue_notices WHERE scope=? AND session_id=? "
                                          "AND state IN ('draft','resolving') ORDER BY rowid DESC LIMIT 1",
                                          (self.scope, row[0])).fetchone()
                if pending:
                    if pending[1] == 'draft':
                        reply += (f'\n\n通知实际状态：草稿 {pending[0]} 尚未触发发送。'
                                  f'确认此草稿请在本话题回复：同意发送 {pending[0]}。')
                    else:
                        return self.recipient_options(pending[0], json.loads(pending[2]))
            return reply
        if (not isinstance(proposal, dict) or not isinstance(proposal.get('body'), str)
                or not proposal['body'].strip() or len(proposal['body']) > 6000
                or not isinstance(proposal.get('recipients'), list) or not 1 <= len(proposal['recipients']) <= 20
                or not all(isinstance(p, dict) and valid_address(p.get('address_as', ''))
                           for p in proposal['recipients'])):
            return reply + '\n\n通知草稿格式无效，未发送。请重新指定对象和内容。'
        try:
            trigger = pipeline_trigger(proposal.get('trigger'), self.host)
        except ValueError:
            return reply + '\n\n流水线触发条件无效，未建立待发送通知。'
        if trigger:
            endpoint, pipeline_id = pipeline_endpoint(trigger)
            try:
                verified = await self.api(endpoint)
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                return reply + '\n\n无法核实指定流水线，未建立待发送通知。'
            if not isinstance(verified, dict) or verified.get('id') != pipeline_id:
                return reply + '\n\n指定流水线与 GitLab 返回结果不一致，未建立待发送通知。'
        key = hashlib.sha256(f'{self.scope}:{source}'.encode()).hexdigest()[:10]
        old = self.db.execute('SELECT payload,state FROM issue_notices WHERE id=?', (key,)).fetchone()
        if old:
            if old[1] == 'resolving':
                return self.recipient_options(key, json.loads(old[0]))
            return reply + '\n\n' + (self.review(key, json.loads(old[0]))
                                     if old[1] == 'draft' else '该通知已处理。')
        payload = {'body': signed(proposal['body'], self.cfg), 'trigger': trigger, 'greeting_version': 1,
                   'recipients': [await self.resolve({'gitlab_id': p.get('gitlab_id'),
                                                      'name': str(p.get('name') or ''),
                                                      'address_as': p.get('address_as', '').strip(),
                                                      'email': str(p.get('email') or '')})
                                  for p in proposal['recipients']]}
        unique = {}
        for person in payload['recipients']:
            identity = person.get('open_id') or person.get('email') or person.get('gitlab_id')
            unique.setdefault(identity or json.dumps(person, sort_keys=True), person)
        payload['recipients'] = list(unique.values())
        prepare_greetings(payload)
        source_row = self.db.execute('SELECT session_id FROM inbox WHERE id=? AND binding=?', (source, self.binding)).fetchone()
        session_id = source_row[0] if source_row else None
        ready = all(p.get('open_id') for p in payload['recipients'])
        # A user may approve an already delivered draft while adding a delay condition.
        # The old, reviewed bytes and recipient IDs must remain identical.
        user_text = self.db.execute('SELECT content FROM inbox WHERE id=? AND binding=?',
                                    (source, self.binding)).fetchone()
        approved = bool(user_text and approves_pipeline_send(user_text[0]))
        if trigger and ready and approved and session_id is not None:
            latest = self.db.execute("SELECT id FROM inbox WHERE binding=? AND session_id=? "
                                     "AND state='done' ORDER BY rowid DESC LIMIT 1",
                                     (self.binding, session_id)).fetchone()
            previous = self.db.execute(
                "SELECT id,source,payload FROM issue_notices WHERE scope=? AND session_id=? "
                "AND state='draft' AND reviewed=1 ORDER BY rowid DESC LIMIT 1",
                (self.scope, session_id)).fetchone()
            if previous and latest and previous[1] == latest[0]:
                old_payload = json.loads(previous[2])
                if notice_messages(old_payload) == notice_messages(payload):
                    old_payload['trigger'] = trigger
                    self.db.execute("UPDATE issue_notices SET payload=?,state='armed' WHERE id=? AND scope=?",
                                    (json.dumps(old_payload, ensure_ascii=False), previous[0], self.scope))
                    self.db.commit()
                    self.reflect_notice(previous[0], '处理中', '通知内容已确认，等待流水线成功',
                                        '等待流水线成功后自动发送')
                    return reply + f'\n\n已按先前审阅的原文和收件人设置通知 {previous[0]}；流水线成功后自动发送。'
        self.db.execute("UPDATE issue_notices SET state='superseded' WHERE scope=? "
                        "AND state IN ('draft','resolving','armed') AND session_id IS ?", (self.scope, session_id))
        self.db.execute('INSERT INTO issue_notices(id,scope,source,payload,state,session_id) VALUES(?,?,?,?,?,?)',
                        (key, self.scope, source, json.dumps(payload, ensure_ascii=False),
                         'draft' if ready else 'resolving', session_id))
        self.db.commit()
        if not ready:
            refs = [ref for ref in data.get('issue_refs', []) if isinstance(ref, str)]
            context = ('\n关联事项：' + '、'.join(refs[:3])) if refs else ''
            return self.recipient_options(key, payload) + context
        return reply + '\n\n' + self.review(key, payload)

    async def verify_sender(self):
        status = json.loads(await self.run([self.cfg['LARK_CLI_BIN'], 'auth', 'status', '--json', '--verify'], 20))
        user = status.get('identities', {}).get('user', {})
        scopes = set(user.get('scope', '').split())
        if (status.get('appId') != self.cfg['FEISHU_APP_ID']
                or user.get('openId') != self.cfg['FEISHU_OWNER_OPEN_ID']
                or user.get('tokenStatus') != 'valid'
                or not {'im:message', 'im:message.send_as_user'} <= scopes):
            raise RuntimeError('用户身份不匹配或缺少 im:message / im:message.send_as_user 授权')

    def reflect_notice(self, key, status, summary, next_step):
        row = self.db.execute('SELECT session_id FROM issue_notices WHERE id=? AND scope=?', (key, self.scope)).fetchone()
        if not row or row[0] is None:
            return
        current = self.db.execute('SELECT summary,next_step FROM sessions WHERE id=? AND binding=?', (row[0], self.binding)).fetchone()
        if not current:
            return
        # Do not mark a different, subsequently resumed task complete just because an older notice was sent.
        notice_steps = {'审阅通知草稿并确认，或补充收件人信息',
                        '审阅通知草稿并确认', '在本话题审阅通知草稿并确认', '选择或补充收件人',
                        '等待流水线成功后自动发送'}
        if status == '已完成' and current[1] not in notice_steps:
            self.db.execute('UPDATE sessions SET summary=? WHERE id=?', (current[0][:130] + '；' + summary, row[0]))
        else:
            self.db.execute('UPDATE sessions SET summary=?,status=?,next_step=? WHERE id=?',
                            (current[0][:130] + '；' + summary, status, next_step, row[0]))
        self.db.commit()

    def same_session(self, key, session_id):
        if session_id is _UNSCOPED:
            return True
        row = self.db.execute('SELECT session_id FROM issue_notices WHERE id=? AND scope=?', (key, self.scope)).fetchone()
        return bool(session_id is not None and row and row[0] == session_id)

    async def confirm(self, key, session_id=_UNSCOPED):
        if not self.same_session(key, session_id):
            return '该通知不属于当前对话，未发送。请进入对应任务话题后确认。'
        row = self.db.execute('SELECT payload,reviewed,state,sent FROM issue_notices WHERE id=? AND scope=?',
                              (key, self.scope)).fetchone()
        if not row:
            return '没有找到这条通知草稿。'
        payload, reviewed, state, sent = row
        payload = json.loads(payload)
        if state == 'resolving':
            return '收件人尚未确认，没有可同意发送的草稿；请先选择或补充收件人。未发送。'
        if state != 'draft':
            return f'该通知状态为 {state}，不会重复发送。发送结果不确定时请先核查飞书。'
        if not reviewed or not all(p.get('open_id') for p in payload['recipients']):
            return '收件人尚未全部匹配，或审阅消息尚未成功送达；未发送。'
        try:
            await self.verify_sender()
        except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
            self.reflect_notice(key, '受阻', '通知发送前身份或授权核验失败', '修复身份或授权后重新确认通知')
            return '未发送：请确认本机登录的是你本人，并授权 im:message 和 im:message.send_as_user，然后重新确认。'
        if payload.get('trigger'):
            changed = self.db.execute("UPDATE issue_notices SET state='armed' WHERE id=? AND scope=? AND state='draft'",
                                      (key, self.scope)).rowcount
            self.db.commit()
            if not changed:
                return '通知状态已变化，未重复设置；请使用 /schedule 查看。'
            self.reflect_notice(key, '处理中', '通知已批准，等待流水线成功', '等待流水线成功后自动发送')
            return f'已批准通知 {key}；流水线成功后将按审阅原文自动发送。'
        return await self.send_notice(key, payload, 'draft')

    async def send_notice(self, key, payload, expected):
        changed = self.db.execute("UPDATE issue_notices SET state='sending' WHERE id=? AND scope=? AND state=?",
                                  (key, self.scope, expected)).rowcount
        self.db.commit()
        if not changed:
            return '通知状态已变化，未重复发送。'
        try:
            for index, p in enumerate(payload['recipients']):
                # Persist progress; never automatically retry an ambiguous user send.
                sendkey = hashlib.sha256(f'{key}:{index}'.encode()).hexdigest()[:40]
                result = json.loads(await self.run([
                    self.cfg['LARK_CLI_BIN'], 'im', '+messages-send', '--as', 'user',
                    '--user-id', p['open_id'], '--text', p.get('body', payload['body']), '--idempotency-key', sendkey], 30))
                if result.get('ok') is not True:
                    raise RuntimeError('User send not confirmed')
                self.db.execute('UPDATE issue_notices SET sent=? WHERE id=?', (index + 1, key))
                if isinstance(p.get('gitlab_id'), int):
                    self.db.execute('INSERT OR REPLACE INTO issue_contacts VALUES(?,?,?)',
                                    (self.scope, p['gitlab_id'], p['email']))
                self.db.commit()
            self.db.execute("UPDATE issue_notices SET state='sent' WHERE id=?", (key,))
            self.db.commit()
            self.reflect_notice(key, '已完成', '通知已按确认内容发送', '')
            result = '已以你的身份发送飞书通知：' + '、'.join(p['email'] for p in payload['recipients'])
            if payload.get('trigger'):
                self.queue_report(key, result)
            return result
        except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
            self.db.execute("UPDATE issue_notices SET state='uncertain' WHERE id=?", (key,))
            self.db.commit()
            count = self.db.execute('SELECT sent FROM issue_notices WHERE id=?', (key,)).fetchone()[0]
            self.reflect_notice(key, '受阻', '通知部分发送或结果不确定', '核查飞书发送结果，避免重复发送')
            result = f'通知发送中断，已确认成功 {count} 人，其余结果可能不确定。请先核查飞书，助手不会自动重发。'
            if payload.get('trigger'):
                self.queue_report(key, result)
            return result

    def queue_report(self, key, message):
        self.db.execute("UPDATE issue_notices SET report_state='pending',report_text=? WHERE id=? AND scope=?",
                        (f'定时通知 {key}：{message}', key, self.scope))
        self.db.commit()

    async def flush_reports(self):
        rows = self.db.execute("SELECT id,session_id,report_text FROM issue_notices "
                               "WHERE scope=? AND report_state='pending'", (self.scope,)).fetchall()
        for key, session_id, message in rows:
            thread = self.db.execute('SELECT root_id FROM session_threads WHERE binding=? AND session_id=?',
                                     (self.binding, session_id)).fetchone() if session_id else None
            args = [self.cfg['LARK_CLI_BIN'], 'im']
            if thread:
                args += ['+messages-reply', '--as', 'bot', '--message-id', thread[0], '--reply-in-thread']
            else:
                args += ['+messages-send', '--as', 'bot', '--chat-id', self.binding]
            changed = self.db.execute("UPDATE issue_notices SET report_state='sending' "
                                      "WHERE id=? AND scope=? AND report_state='pending'", (key, self.scope)).rowcount
            self.db.commit()
            if not changed:
                continue
            try:
                result = json.loads(await self.run(args + ['--markdown', message,
                    '--idempotency-key', hashlib.sha256(f'notice-report:{key}'.encode()).hexdigest()[:40]], 30))
                from assistant.service import sent_message_id
                if result.get('ok') is not True or not sent_message_id(result):
                    raise RuntimeError('Owner report not confirmed')
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                self.db.execute("UPDATE issue_notices SET report_state='uncertain' WHERE id=?", (key,))
            else:
                self.db.execute("UPDATE issue_notices SET report_state='sent' WHERE id=?", (key,))
            self.db.commit()

    async def tick_deferred(self):
        rows = self.db.execute("SELECT id,session_id,payload FROM issue_notices WHERE scope=? AND state='armed'",
                               (self.scope,)).fetchall()
        for key, session_id, raw in rows:
            if session_id and self.db.execute(
                    "SELECT 1 FROM inbox WHERE binding=? AND session_id=? AND state='pending' LIMIT 1",
                    (self.binding, session_id)).fetchone():
                continue
            payload = json.loads(raw)
            trigger = pipeline_trigger(payload.get('trigger'), self.host)
            if not trigger:
                continue
            endpoint, pipeline_id = pipeline_endpoint(trigger)
            try:
                pipeline = await self.api(endpoint)
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                logging.warning('Deferred pipeline check failed for notice %s', key)
                continue
            if not isinstance(pipeline, dict) or pipeline.get('id') != pipeline_id:
                logging.warning('Deferred pipeline response mismatch for notice %s', key)
                continue
            status = pipeline.get('status')
            self.db.execute('UPDATE issue_notices SET checked_at=?,trigger_status=? WHERE id=? AND scope=?',
                            (now_local(self.cfg).timestamp(), status, key, self.scope))
            self.db.commit()
            if status == 'success':
                try:
                    await self.verify_sender()
                except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                    changed = self.db.execute("UPDATE issue_notices SET state='blocked' "
                                              "WHERE id=? AND scope=? AND state='armed'", (key, self.scope)).rowcount
                    self.db.commit()
                    if not changed:
                        continue
                    self.reflect_notice(key, '受阻', '流水线已成功，但发送身份或授权核验失败', '修复授权后核查通知')
                    self.queue_report(key, '流水线已成功，但飞书发送身份或授权核验失败，消息未发送。')
                    continue
                await self.send_notice(key, payload, 'armed')
            elif status in {'failed', 'canceled', 'skipped'}:
                changed = self.db.execute("UPDATE issue_notices SET state='condition_failed' "
                                          "WHERE id=? AND scope=? AND state='armed'", (key, self.scope)).rowcount
                self.db.commit()
                if not changed:
                    continue
                self.reflect_notice(key, '受阻', f'流水线状态为 {status}，未发送通知', '核查流水线并决定后续处理')
                self.queue_report(key, f'流水线状态为 {status}，原定消息未发送。')
        await self.flush_reports()

    async def deferred_scheduler(self):
        while True:
            try:
                await self.tick_deferred()
            except (RuntimeError, ValueError, OSError, KeyError, TypeError, asyncio.TimeoutError) as error:
                logging.warning('Deferred notice scheduler failed (%s)', type(error).__name__)
            await asyncio.sleep(30)

    async def handle_command(self, text, source, session_id=_UNSCOPED, review_source=None):
        text = text.strip()
        text = strip_bot_mention(text, self.cfg)
        if text.casefold().rstrip('。.!！') in {'同意', 'ok', '好的', '同意发送', '可以发送'}:
            drafts = self.db.execute("SELECT id,source FROM issue_notices WHERE scope=? AND state='draft'",
                                     (self.scope,)).fetchall()
            all_drafts = drafts
            if session_id is not _UNSCOPED:
                drafts = [r for r in drafts if self.same_session(r[0], session_id)]
            latest = self.db.execute("SELECT id FROM inbox WHERE binding=? AND state='done' "
                                     "AND (? OR session_id=?) ORDER BY rowid DESC LIMIT 1",
                                     (self.binding, session_id is _UNSCOPED,
                                      None if session_id is _UNSCOPED else session_id)).fetchone()
            # Short approvals apply only to the immediately preceding delivered review.
            # An unrelated conversation must not accidentally approve an older draft.
            reviewed_source = review_source or (latest[0] if latest else None)
            if len(drafts) == 1 and drafts[0][1] == reviewed_source:
                return await self.confirm(drafts[0][0], session_id)
            unresolved = self.db.execute("SELECT id FROM issue_notices WHERE scope=? AND state='resolving' "
                                         "AND (? OR session_id=?) LIMIT 1",
                                         (self.scope, session_id is _UNSCOPED,
                                          None if session_id is _UNSCOPED else session_id)).fetchone()
            if unresolved:
                return '未发送：收件人尚未确认，没有可同意发送的草稿。请先选择或补充收件人。'
            if session_id is not _UNSCOPED and all_drafts:
                return '未发送：请在对应任务话题内引用通知草稿，或使用“同意发送 通知编号”明确确认。'
        if text == '/issue':
            return await self.full_refresh(source=source)
        match = re.fullmatch(r'(同意发送|取消通知) ([0-9a-f]{10})', text)
        if match:
            action, key = match.groups()
            if not self.same_session(key, session_id):
                return '该通知不属于当前对话，请进入对应任务话题。'
            if action == '同意发送':
                return await self.confirm(key, session_id)
            changed = self.db.execute("UPDATE issue_notices SET state='cancelled' WHERE id=? AND scope=? "
                                      "AND state IN ('draft','resolving','armed')",
                                      (key, self.scope)).rowcount
            self.db.commit()
            if changed:
                self.reflect_notice(key, '已完成', '通知草稿已取消', '')
            return '已取消尚未发送的通知草稿。' if changed else '没有找到可取消的通知草稿。'
        match = re.fullmatch(r'邮箱 ([0-9a-f]{10}) (\d+) (\S+)', text)
        if match:
            key, index, email = match.groups()
            return await self.add_email(key, int(index)-1, email, source, session_id)
        match = re.fullmatch(r'选择收件人 ([0-9a-f]{10}) (\d+) (\d+)', text)
        if match:
            key, index, choice = match.groups()
            return await self.choose_recipient(key, int(index)-1, int(choice)-1, source, session_id)
        if EMAIL.fullmatch(text):
            unresolved = []
            for key, raw in self.db.execute("SELECT id,payload FROM issue_notices WHERE scope=? AND state='resolving'", (self.scope,)):
                if not self.same_session(key, session_id):
                    continue
                unresolved.extend((key, index) for index, p in enumerate(json.loads(raw)['recipients']) if not p.get('open_id'))
            if len(unresolved) == 1:
                return await self.add_email(*unresolved[0], text, source, session_id)
            if unresolved:
                return '存在多个未匹配对象，请使用：邮箱 通知编号 对象序号 同事邮箱。'
        return None

    def update_recipients(self, key, payload, source):
        seen_ids = set()
        recipients = []
        for person in payload['recipients']:
            if person.get('open_id') and person['open_id'] in seen_ids:
                continue
            if person.get('open_id'):
                seen_ids.add(person['open_id'])
            recipients.append(person)
        payload['recipients'] = recipients
        prepare_greetings(payload)
        ready = all(p.get('open_id') for p in recipients)
        self.db.execute('UPDATE issue_notices SET payload=?,reviewed=0,source=?,state=? WHERE id=?',
                        (json.dumps(payload, ensure_ascii=False), source,
                         'draft' if ready else 'resolving', key))
        self.db.commit()
        self.reflect_notice(key, '等待回复', '收件人信息已更新，等待重新审阅' if ready else '等待确认收件人',
                            '审阅通知草稿并确认' if ready else '选择或补充收件人')
        return self.review(key, payload)

    async def choose_recipient(self, key, index, choice, source, session_id=_UNSCOPED):
        if not self.same_session(key, session_id):
            return '该通知不属于当前对话，请进入对应任务话题。'
        row = self.db.execute("SELECT payload FROM issue_notices WHERE id=? AND scope=? AND state='resolving'",
                              (key, self.scope)).fetchone()
        if not row:
            return '没有等待选择收件人的请求。'
        payload = json.loads(row[0])
        if not 0 <= index < len(payload['recipients']):
            return '对象序号无效。'
        person = payload['recipients'][index]
        candidates = person.get('candidates') or []
        if person.get('open_id') or not 0 <= choice < len(candidates):
            return '候选序号无效。'
        candidate = candidates[choice]
        if not re.fullmatch(r'ou_[A-Za-z0-9_]+', str(candidate.get('open_id') or '')):
            return '候选用户 ID 无效，请重新搜索。'
        if isinstance(person.get('gitlab_id'), int):
            email = candidate.get('email') or ''
            if not EMAIL.fullmatch(email):
                return '该候选人的邮箱不可见，无法核实 GitLab 身份；请补充邮箱。'
            resolved = await self.resolve({'gitlab_id': person['gitlab_id'],
                                           'address_as': person.get('address_as', ''),
                                           'name': person.get('name'), 'email': email})
            if resolved.get('open_id') != candidate['open_id']:
                return '所选飞书用户与邮箱复查结果不一致，未生成草稿。请补充正确邮箱。'
        else:
            resolved = {**person, 'email': candidate.get('email') or '',
                        'open_id': candidate['open_id'], 'feishu_name': candidate.get('name') or '',
                        'department': candidate.get('department') or '',
                        'match_source': '你从飞书通讯录候选中明确选择；请核对收件人'}
            resolved.pop('error', None)
            resolved.pop('candidates', None)
        payload['recipients'][index] = resolved
        return self.update_recipients(key, payload, source)

    async def add_email(self, key, index, email, source, session_id=_UNSCOPED):
        if not self.same_session(key, session_id):
            return '该通知不属于当前对话，请进入对应任务话题。'
        row = self.db.execute("SELECT payload FROM issue_notices WHERE id=? AND scope=? "
                              "AND state IN ('draft','resolving')", (key, self.scope)).fetchone()
        if not row or not EMAIL.fullmatch(email):
            return '通知草稿不存在或邮箱格式无效。'
        payload = json.loads(row[0])
        if not 0 <= index < len(payload['recipients']):
            return '对象序号无效。'
        person = payload['recipients'][index]
        payload['recipients'][index] = await self.resolve({'gitlab_id': person.get('gitlab_id'),
                                                         'address_as': person.get('address_as', ''),
                                                         'name': person.get('name'), 'email': email})
        return self.update_recipients(key, payload, source)
