"""Persistent conversation routing and command handling for the bound private chat."""
import json
import re
import time

STATUSES = ('处理中', '等待回复', '已完成', '受阻')
HINT = '*请进入对应话题继续任务；在主聊天发送新任务会创建新话题。*'


class ThreadError(ValueError):
    pass


def brief(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def command(text):
    value = text.strip()
    if value.casefold() == 'help':
        return 'help', ''
    match = re.match(r'^/([a-z]+)(?:\s+(.*))?$', value, re.I | re.S)
    if match:
        return match[1].casefold(), (match[2] or '').strip()
    if value.startswith('/'):
        return 'unknown', ''
    return None


def followup(text):
    return bool(re.fullmatch(r'(继续(?:处理|执行|吧)?|好的?|同意(?:发送)?|确认(?:发送|删除|执行)?|收到|没问题|可以(?:发送)?|ok|再试(?:一下)?|然后呢|为什么|再看看|再(?:分析|检查)(?:一下)?)[。.!！?？]*',
                             text.strip(), re.I))


def initialize(db):
    for column, kind in [('session_id', 'INTEGER'), ('route_reason', 'TEXT'), ('reply_version', 'INTEGER DEFAULT 1'), ('fast_lane', 'INTEGER DEFAULT 0'), ('thread_id', 'TEXT'), ('root_id', 'TEXT'), ('thread_checked', 'INTEGER NOT NULL DEFAULT 0')]:
        if column not in {r[1] for r in db.execute('PRAGMA table_info(inbox)')}:
            db.execute(f'ALTER TABLE inbox ADD COLUMN {column} {kind}')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, binding TEXT NOT NULL, title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '等待回复',
            next_step TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
            source TEXT UNIQUE);
        CREATE INDEX IF NOT EXISTS sessions_recent ON sessions(binding,updated DESC,id DESC);
        CREATE TABLE IF NOT EXISTS session_history (
            seq INTEGER PRIMARY KEY, binding TEXT NOT NULL, session_id INTEGER NOT NULL,
            source TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            UNIQUE(binding,source,role));
        CREATE INDEX IF NOT EXISTS session_history_lookup ON session_history(binding,session_id,seq);
        CREATE TABLE IF NOT EXISTS session_messages (
            binding TEXT NOT NULL, message_id TEXT NOT NULL, session_id INTEGER NOT NULL,
            source TEXT NOT NULL, content TEXT NOT NULL, role TEXT NOT NULL,
            PRIMARY KEY(binding,message_id));
        CREATE TABLE IF NOT EXISTS session_threads (
            binding TEXT NOT NULL, root_id TEXT NOT NULL, thread_id TEXT,
            session_id INTEGER, link TEXT, deleted_at REAL,
            PRIMARY KEY(binding,root_id), UNIQUE(binding,thread_id), UNIQUE(binding,session_id));
    ''')
    columns = {r[1] for r in db.execute('PRAGMA table_info(sessions)')}
    for column, kind in [('saved', 'INTEGER NOT NULL DEFAULT 0'), ('last_used', 'REAL')]:
        if column not in columns:
            db.execute(f'ALTER TABLE sessions ADD COLUMN {column} {kind}')
    db.execute('UPDATE sessions SET last_used=updated WHERE last_used IS NULL')
    db.executescript("""
        CREATE TABLE IF NOT EXISTS clean_plans (
            token TEXT PRIMARY KEY, binding TEXT NOT NULL, source TEXT UNIQUE NOT NULL,
            policy INTEGER NOT NULL, candidates TEXT NOT NULL, created REAL NOT NULL,
            preview TEXT NOT NULL, result TEXT);
        CREATE TABLE IF NOT EXISTS deleted_session_messages (
            binding TEXT NOT NULL, message_id TEXT NOT NULL, deleted_at REAL NOT NULL,
            PRIMARY KEY(binding,message_id));
        CREATE TABLE IF NOT EXISTS bot_health (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    db.commit()


class Sessions:
    def __init__(self, db, binding):
        self.db, self.binding = db, binding

    def get(self, sid):
        if type(sid) is not int or not 0 < sid < 2**63:
            return None
        row = self.db.execute('SELECT id,title,summary,status,next_step,updated,saved,last_used FROM sessions WHERE id=? AND binding=?',
                              (sid, self.binding)).fetchone()
        return dict(zip(('id', 'title', 'summary', 'status', 'next_step', 'updated', 'saved', 'last_used'), row)) if row else None

    def recent(self, page=1):
        ids = self.db.execute('SELECT id FROM sessions WHERE binding=? ORDER BY updated DESC,id DESC LIMIT 10 OFFSET ?',
                              (self.binding, (page - 1) * 10)).fetchall()
        return [self.get(r[0]) for r in ids]

    def thread(self, sid):
        if type(sid) is not int or not 0 < sid < 2**63:
            return None
        row = self.db.execute('SELECT root_id,thread_id,link FROM session_threads WHERE binding=? AND session_id=?',
                              (self.binding, sid)).fetchone()
        return dict(zip(('root_id', 'thread_id', 'link'), row)) if row else None

    def bind(self, sid, root_id, thread_id=None, link=None):
        if not self.get(sid):
            raise ThreadError('会话不存在或已清理，请在主聊天开始新任务。')
        by_root = self.db.execute('SELECT session_id,thread_id FROM session_threads WHERE binding=? AND root_id=?',
                                 (self.binding, root_id)).fetchone()
        existing = self.thread(sid)
        if (by_root and (by_root[0] != sid or (thread_id and by_root[1] and by_root[1] != thread_id))
                or existing and existing['root_id'] != root_id):
            raise ThreadError('话题绑定冲突，未切换或合并会话。')
        if thread_id:
            other = self.db.execute('SELECT session_id FROM session_threads WHERE binding=? AND thread_id=?',
                                    (self.binding, thread_id)).fetchone()
            if other and other[0] != sid:
                raise ThreadError('话题已绑定其他会话或已清理。')
        self.db.execute('INSERT INTO session_threads(binding,root_id,thread_id,session_id,link) VALUES(?,?,?,?,?) '
                        'ON CONFLICT(binding,root_id) DO UPDATE SET thread_id=coalesce(excluded.thread_id,thread_id),'
                        'link=coalesce(excluded.link,link)', (self.binding, root_id, thread_id, sid, link))

    def context(self, thread_id, root_id=None):
        """Resolve only actual thread metadata; quotes never select a session."""
        if not thread_id:
            return None
        row = self.db.execute('SELECT session_id,root_id FROM session_threads WHERE binding=? AND thread_id=?',
                              (self.binding, thread_id)).fetchone()
        if row:
            if row[0] is None or not self.get(row[0]):
                raise ThreadError('此话题的本地上下文已清理，不会恢复旧历史；请在主聊天开始新任务。')
            if root_id and row[1] != root_id:
                raise ThreadError('话题与根消息不一致，未处理任务。')
            return row[0]
        if not root_id:
            raise ThreadError('无法确认话题根消息，未猜测会话归属；请稍后重试。')
        if self.deleted_reference(root_id):
            raise ThreadError('此话题的本地上下文已清理，请在主聊天开始新任务。')
        pending = self.db.execute('SELECT session_id,thread_id FROM session_threads WHERE binding=? AND root_id=?',
                                  (self.binding, root_id)).fetchone()
        if pending:
            if pending[0] is None:
                raise ThreadError('此话题的本地上下文已清理，请在主聊天开始新任务。')
            self.bind(pending[0], root_id, thread_id)
            return pending[0]
        return None

    def entry(self, sid):
        item = self.thread(sid)
        if not item:
            return '此会话没有话题入口，请在主聊天开始新任务。'
        if item['link']:
            return f'[进入话题]({item["link"]})'
        return '话题入口暂不可用，请在原话题继续；发送 /session 编号可重试获取入口'

    def create(self, source, title='新对话'):
        old = self.db.execute('SELECT id FROM sessions WHERE binding=? AND source=?', (self.binding, source)).fetchone()
        if old:
            return old[0]
        now = time.time()
        return self.db.execute('INSERT INTO sessions(binding,title,created,updated,source,last_used) VALUES(?,?,?,?,?,?)',
                               (self.binding, brief(title, 40) or '新对话', now, now, source, now)).lastrowid

    def deleted_reference(self, message_id):
        return bool(message_id and self.db.execute('SELECT 1 FROM deleted_session_messages WHERE binding=? AND message_id=?',
                                                   (self.binding, message_id)).fetchone())

    def reference(self, message_id):
        if not message_id:
            return None
        row = self.db.execute('SELECT session_id,source,content,role FROM session_messages WHERE binding=? AND message_id=?',
                              (self.binding, message_id)).fetchone()
        if row and self.get(row[0]):
            return dict(zip(('session_id', 'source', 'content', 'role'), row))
        return None

    def map_message(self, mid, sid, source, content, role):
        if sid and self.get(sid):
            self.db.execute('INSERT OR IGNORE INTO session_messages VALUES(?,?,?,?,?,?)',
                            (self.binding, mid, sid, source, content, role))

    def handle(self, name, argument, source):
        """Directory commands do not select a global current session."""
        if name != 'session':
            return '不支持此命令，请使用 /help 查看用法。', None, None
        page_match = re.fullmatch(r'page\s+([1-9]\d{0,6})', argument, re.I)
        if not argument or page_match:
            page = int(page_match[1]) if page_match else 1
            lines = [f'#{s["id"]} {brief(s["title"], 28)}｜{s["status"]}'
                     + ('｜⭐ 已保存' if s['saved'] else '') + '\n' + self.entry(s['id'])
                     for s in self.recent(page)]
            count = self.db.execute('SELECT count(*) FROM sessions WHERE binding=?', (self.binding,)).fetchone()[0]
            pages = max(1, (count + 9) // 10)
            return ('\n\n'.join(lines or ['暂无对话。']) + f'\n\n第 {page}/{pages} 页；/session page 页码\n' + HINT,
                    None, None)
        data = self.get(int(argument.lstrip('#'))) if re.fullmatch(r'#?\d{1,19}', argument) else None
        if not data:
            return '未找到该对话。用法：/session [编号] 或 /session page 页码', None, None
        text = (f'#{data["id"]} {brief(data["title"], 35)}' + ('｜⭐ 已保存' if data['saved'] else '')
                + f'\n进展：{brief(data["summary"], 180) or "尚未开始"}'
                + f'\n最后状态：{data["status"]}\n下一步：{brief(data["next_step"], 100) or "暂无"}'
                + '\n' + self.entry(data['id']))
        return text + '\n\n' + HINT, None, None

    def route(self, source, text, thread_id=None, root_id=None):
        existing = self.db.execute('SELECT session_id,route_reason FROM inbox WHERE id=? AND binding=?',
                                   (source, self.binding)).fetchone()
        if existing and existing[0] and self.get(existing[0]):
            sid = existing[0]
            if not self.thread(sid):
                self.bind(sid, root_id if thread_id and root_id else source, thread_id)
            return sid, existing[1] or 'resume'
        sid = self.context(thread_id, root_id)
        reason = 'thread' if sid else 'new'
        if sid is None:
            if not thread_id and followup(text):
                raise ThreadError('请进入对应话题继续任务；可用 /session 查找。主聊天中的简短确认不会自动归入旧任务。')
            sid = self.create(source, text)
            self.bind(sid, root_id if thread_id else source, thread_id)
        self.db.execute('UPDATE sessions SET last_used=? WHERE id=?', (time.time(), sid))
        self.db.execute('UPDATE inbox SET session_id=?,route_reason=? WHERE id=? AND binding=?',
                        (sid, reason, source, self.binding))
        self.map_message(source, sid, source, text, 'user')
        self.db.commit()
        return sid, reason

    def history(self, sid, turns):
        return self.db.execute('SELECT role,content FROM (SELECT seq,role,content FROM session_history '
                               'WHERE binding=? AND session_id=? ORDER BY seq DESC LIMIT ?) ORDER BY seq',
                               (self.binding, sid, turns*2)).fetchall()

    def finish(self, sid, source, text, reply, metadata=None, failed=False):
        for role, content in [('user', text), ('assistant', reply)]:
            self.db.execute('INSERT OR IGNORE INTO session_history(binding,session_id,source,role,content) VALUES(?,?,?,?,?)',
                            (self.binding, sid, source, role, content))
        data = metadata or {}
        previous = self.get(sid)
        if failed:
            data = {**data, 'summary': brief(previous['summary'], 140) + '；本轮处理失败或超时。'}
        status = data.get('status') if data.get('status') in STATUSES else '等待回复'
        self.db.execute('UPDATE sessions SET title=?,summary=?,status=?,next_step=?,updated=?,last_used=? WHERE id=? AND binding=?',
                        (brief(data.get('title') or self.get(sid)['title'], 40),
                         brief(data.get('summary') or reply, 180), '受阻' if failed else status,
                         brief(data.get('next_step') or ('请稍后重试' if failed else ''), 100),
                         time.time(), time.time(), sid, self.binding))
        self.map_message(source, sid, source, text, 'user')


def session_schema():
    return {'type': 'object', 'properties': {
        'title': {'type': 'string'}, 'summary': {'type': 'string'},
        'status': {'type': 'string', 'enum': list(STATUSES)}, 'next_step': {'type': 'string'}},
        'required': ['title', 'summary', 'status', 'next_step'], 'additionalProperties': False}
