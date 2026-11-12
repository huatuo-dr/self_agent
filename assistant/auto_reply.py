"""Owner-controlled automatic replies to verified Feishu messages."""

import json
import re
import time

from assistant.settings import sign_message


MAX_CONTEXT = 8
CONTEXT_TTL = 7 * 86400
SAFE_TYPES = {'text', 'post'}
SECRET = re.compile(r'(?i)(?:password|passwd|secret|token|api[_ -]?key|private[_ -]?key)\s*[:=]|'
                    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{16,})\b')
PERSONAL = re.compile(r'(?i)\b[\w.+-]+@[\w.-]+\.[A-Z]{2,}\b|(?<!\d)1[3-9]\d{9}(?!\d)|'
                      r'(?<!\d)\d{17}[\dXx](?!\d)')
TOKENISH = re.compile(r'(?i)(?:\b(?:bearer|basic)\s+|\b(?:glpat-|ghp_))[A-Za-z0-9+/._=-]{8,}')
JWT = re.compile(r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{5,})?\b')
URL_CREDENTIAL = re.compile(r'(?i)https?://[^/\s:@]+:[^@\s/]+@')
EXECUTION_CLAIM = re.compile(r'已(?:经)?(?:执行|安装|删除|修改|部署|合并|批准|发送|重试)')


class AdapterUnavailable(RuntimeError):
    pass


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS auto_control (
            binding TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            generation INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
            disabled_reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS auto_cursor (
            binding TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS auto_messages (
            binding TEXT NOT NULL, message_id TEXT NOT NULL, chat_id TEXT NOT NULL,
            conversation TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
            body TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL,
            reply_id TEXT NOT NULL DEFAULT '', notice_id TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            PRIMARY KEY(binding,message_id));
        CREATE INDEX IF NOT EXISTS auto_pending ON auto_messages(binding,state,created);
        CREATE TABLE IF NOT EXISTS auto_context (
            binding TEXT NOT NULL, conversation TEXT NOT NULL, message_id TEXT NOT NULL,
            role TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(binding,conversation,message_id,role));
        CREATE INDEX IF NOT EXISTS auto_context_recent ON auto_context(binding,conversation,created);
        CREATE TABLE IF NOT EXISTS auto_errors (
            binding TEXT NOT NULL, stage TEXT NOT NULL, error_type TEXT NOT NULL,
            created REAL NOT NULL);
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(auto_control)')}
    if 'disabled_reason' not in columns:
        db.execute("ALTER TABLE auto_control ADD COLUMN disabled_reason TEXT NOT NULL DEFAULT ''")
    db.commit()


def safe_result(text, *, public=False, verified_source=False):
    """Fail closed for ambiguous internal data. Public provenance is mandatory."""
    return (isinstance(text, str) and 0 < len(text) <= 4000 and public
            and not SECRET.search(text) and not PERSONAL.search(text) and not TOKENISH.search(text)
            and not JWT.search(text)
            and not URL_CREDENTIAL.search(text)
            and (verified_source or not EXECUTION_CLAIM.search(text)))


def salutation(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('收件人称呼未经核实')
    label = re.split(r'[（(]', name.strip(), maxsplit=1)[0].strip()
    if not label or any(x in label for x in ('\n', '\r', '@')) or SECRET.search(label) or PERSONAL.search(label):
        raise ValueError('收件人称呼未经核实')
    return f'你好，{label}'


def conversation_key(message):
    chat = message['chat_id']
    if message['chat_type'] == 'p2p':
        return 'dm:' + chat
    # A thread identifier is required. A new root message starts its own topic.
    topic = message.get('root_id') or message.get('thread_id') or message['message_id']
    return 'group:' + chat + ':' + topic


def eligible(message, owner_id, owner_chat, watched_chats):
    """Return a reason when identity, body or structured mention cannot be proven."""
    if not isinstance(message, dict) or not all(isinstance(message.get(k), str) and message[k]
                                                for k in ('message_id', 'chat_id', 'chat_type', 'sender_id')):
        return 'identity_unknown'
    if any(message.get(key) is not True for key in
           ('source_verified', 'sender_verified', 'body_verified')):
        return 'provenance_unknown'
    if message['chat_id'] == owner_chat:
        return 'owner'
    try:
        salutation(message.get('sender_name'))
    except ValueError:
        return 'sender_name_unknown'
    if message.get('sender_type') != 'user':
        return 'sender_unknown_or_bot'
    if message.get('message_type') not in SAFE_TYPES:
        return 'body_unknown'
    body = message.get('body')
    if not isinstance(body, str) or not body.strip() or len(body) > 6000:
        return 'body_unknown'
    # Only the newly sent body is inspected; quoted and forwarded data are separate.
    if body.lstrip().startswith('/') or body.strip().casefold() == 'help':
        return 'command'
    if message['chat_type'] == 'p2p':
        return 'owner' if message['sender_id'] == owner_id else None
    if message['chat_type'] != 'group' or message['chat_id'] in watched_chats:
        return 'watched_or_unknown_chat'
    mentions = message.get('mentions')
    if not isinstance(mentions, list):
        return 'mentions_unknown'
    owner_found = False
    for mention in mentions:
        if not isinstance(mention, dict):
            return 'mentions_unknown'
        if mention.get('all') is True or mention.get('type') in ('all', 'at_all'):
            return 'everyone'
        identity = mention.get('id')
        if isinstance(identity, dict):
            identity = identity.get('open_id')
        if identity in ('all', '@all'):
            return 'everyone'
        if not isinstance(identity, str):
            return 'mentions_unknown'
        owner_found |= identity == owner_id
    return None if owner_found else 'owner' if message['sender_id'] == owner_id else 'not_mentioned'


class AutoReply:
    def __init__(self, db, owner_id, binding, adapter, query=None, cfg=None):
        self.cfg = cfg or {}
        self.db, self.owner_id, self.binding = db, owner_id, binding
        self.adapter = adapter
        self.query = query
        self.discovery_failures = 0
        self.discovery_retry_at = 0.0
        initialize(db)
        with db:
            db.execute('INSERT OR IGNORE INTO auto_control(binding,updated) VALUES(?,?)',
                       (binding, time.time()))

    def control(self):
        row = self.db.execute('SELECT enabled,generation FROM auto_control WHERE binding=?',
                              (self.binding,)).fetchone()
        return tuple(row)

    def active(self):
        return bool(self.control()[0])

    async def enable(self):
        """Check the live transport before acknowledging an owner enable request."""
        if self.active():
            return '/auto 已在运行。'
        if self.query is None:
            return '/auto 启动失败：只读问答未接入；保持关闭。'
        start = int(time.time())
        try:
            await self.query.preflight()
            features = await self.adapter.preflight()
        except Exception as error:
            reason = str(error) if isinstance(error, AdapterUnavailable) else type(error).__name__
            with self.db:
                self.db.execute('UPDATE auto_control SET enabled=0,disabled_reason=?,updated=? WHERE binding=?',
                                (reason[:100], time.time(), self.binding))
            return '/auto 启动失败：' + reason[:100] + '；保持关闭。'
        with self.db:
            self.db.execute("UPDATE auto_control SET enabled=1,disabled_reason='',updated=? WHERE binding=?",
                            (time.time(), self.binding))
            self.db.execute('INSERT INTO auto_cursor VALUES(?,?,?) ON CONFLICT(binding) DO UPDATE '
                            'SET cursor=excluded.cursor,updated=excluded.updated',
                            (self.binding, json.dumps({'since': start}), time.time()))
        self.discovery_failures = 0
        self.discovery_retry_at = 0.0
        response = '/auto 已开启，开始收取新消息。'
        reaction_ready = features.get('reaction_ready') if isinstance(features, dict) else None
        if reaction_ready is False:
            response += '消息表情权限尚未就绪；问答仍可运行。'
        return response

    async def stop_for_failure(self, stage, error, *, notify=True):
        if not self.active():
            return
        reason = str(error) if isinstance(error, AdapterUnavailable) else type(error).__name__
        reason = (stage + '：' + reason)[:100]
        with self.db:
            self.db.execute('UPDATE auto_control SET enabled=0,generation=generation+1,'
                            'disabled_reason=?,updated=? WHERE binding=?', (reason, time.time(), self.binding))
            self.db.execute("UPDATE auto_messages SET state='cancelled',reason='auto stopped' "
                            "WHERE binding=? AND state IN ('pending','prepared')", (self.binding,))
        if notify:
            try:
                await self.adapter.notify_owner('/auto 已因故停止：' + reason)
            except Exception as notice_error:
                self._failure('stop_notice_failed', type(notice_error).__name__)

    def command(self, argument):
        value = argument.strip().casefold()
        if value == 'status':
            if self.active():
                return '/auto：运行中'
            row = self.db.execute('SELECT disabled_reason FROM auto_control WHERE binding=?',
                                  (self.binding,)).fetchone()
            return '/auto：已关闭' + ('；原因：' + row[0] if row and row[0] else '')
        if value == 'on':
            return '/auto on 需要先检查飞书权限；请通过 bot 私聊发送该命令。'
        if value == 'off':
            with self.db:
                self.db.execute("UPDATE auto_control SET enabled=0,generation=generation+1,"
                                "disabled_reason='',updated=? WHERE binding=?",
                                (time.time(), self.binding))
                self.db.execute("UPDATE auto_messages SET state='cancelled',reason='auto off' "
                                "WHERE binding=? AND state IN ('pending','prepared')", (self.binding,))
            return '/auto 已关闭；未发送代答已取消。'
        return '用法：/auto on、/auto off 或 /auto status'

    def watched_chats(self):
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_watches'").fetchone()
        if not exists:
            return set()
        return {row[0] for row in self.db.execute(
            "SELECT chat_id FROM group_watches WHERE binding=? AND state='active'", (self.binding,))}

    def ingest(self, message):
        if not self.active():
            return False
        # A reply sent with the owner's identity can appear in the next scan.
        # Its recorded ID must never become a fresh self-mention request.
        if isinstance(message, dict) and isinstance(message.get('message_id'), str) and self.db.execute(
                'SELECT 1 FROM auto_messages WHERE binding=? AND reply_id=? LIMIT 1',
                (self.binding, message['message_id'])).fetchone():
            return False
        reason = eligible(message, self.owner_id, self.binding, self.watched_chats())
        if reason:
            return False
        generation = self.control()[1]
        with self.db:
            cursor = self.db.execute(
                'INSERT OR IGNORE INTO auto_messages '
                '(binding,message_id,chat_id,conversation,sender_id,sender_name,body,state,generation,created) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.binding, message['message_id'], message['chat_id'], conversation_key(message),
                 message['sender_id'], message['sender_name'], message['body'], 'pending', generation, time.time()))
        return cursor.rowcount == 1

    def context(self, conversation, sender_id=None):
        cutoff = time.time() - CONTEXT_TTL
        rows = self.db.execute('SELECT c.role,c.body FROM auto_context c WHERE c.binding=? AND c.conversation=? '
                               'AND c.created>=? AND (? IS NULL OR EXISTS (SELECT 1 FROM auto_messages m '
                               'WHERE m.binding=c.binding AND m.sender_id=? AND '
                               '(m.message_id=c.message_id OR m.reply_id=c.message_id))) '
                               'ORDER BY c.created DESC LIMIT ?',
                               (self.binding, conversation, cutoff, sender_id, sender_id, MAX_CONTEXT)).fetchall()
        return list(reversed(rows))

    def remember(self, conversation, message_id, role, body):
        with self.db:
            self.db.execute('DELETE FROM auto_context WHERE created<?', (time.time() - CONTEXT_TTL,))
            self.db.execute('INSERT OR IGNORE INTO auto_context VALUES(?,?,?,?,?,?)',
                            (self.binding, conversation, message_id, role, body[:4000], time.time()))
            self.db.execute('DELETE FROM auto_context WHERE binding=? AND conversation=? AND rowid NOT IN '
                            '(SELECT rowid FROM auto_context WHERE binding=? AND conversation=? '
                            'ORDER BY created DESC LIMIT ?)',
                            (self.binding, conversation, self.binding, conversation, MAX_CONTEXT))

    async def poll_once(self):
        if not self.active():
            return 0
        if time.monotonic() < self.discovery_retry_at:
            return 0
        generation = self.control()[1]
        row = self.db.execute('SELECT cursor FROM auto_cursor WHERE binding=?', (self.binding,)).fetchone()
        hot_count = 0
        async def handle_hot(batch):
            nonlocal hot_count
            if not self.active() or generation != self.control()[1]:
                return
            hot_count += sum(self.ingest(item) for item in batch)
            for _ in range(min(len(batch), 5)):
                if not self.active() or not await self.process_once():
                    break
        try:
            hot = (await self.adapter.discover_hot(row[0] if row else '', on_messages=handle_hot)
                   if hasattr(self.adapter, 'discover_hot') else [])
            messages, cursor = await self.adapter.discover(row[0] if row else '')
            if not isinstance(hot, list) or not isinstance(messages, list) or not isinstance(cursor, str):
                raise ValueError('消息发现结果格式无效')
        except Exception as error:
            self._failure('discovery_failed', type(error).__name__)
            if generation == self.control()[1]:
                self.discovery_failures += 1
                if self.discovery_failures >= 3:
                    await self.stop_for_failure('消息发现连续失败 3 次', error)
                else:
                    self.discovery_retry_at = time.monotonic() + (5 if self.discovery_failures == 1 else 20)
            return 0
        if not self.active() or generation != self.control()[1]:
            return 0
        self.discovery_failures = 0
        self.discovery_retry_at = 0.0
        count = hot_count + sum(self.ingest(item) for item in hot + messages)
        with self.db:
            self.db.execute('INSERT INTO auto_cursor VALUES(?,?,?) ON CONFLICT(binding) DO UPDATE '
                            'SET cursor=excluded.cursor,updated=excluded.updated',
                            (self.binding, cursor, time.time()))
        return count

    def _failure(self, state, reason):
        # Keep diagnostics short and free of possible remote response content.
        with self.db:
            self.db.execute('INSERT INTO auto_errors VALUES(?,?,?,?)',
                            (self.binding, state, reason[:80], time.time()))

    def recover(self):
        # An interrupted external call may already have succeeded.
        with self.db:
            self.db.execute("UPDATE auto_control SET enabled=0,generation=generation+1,"
                            "disabled_reason='服务重启后默认关闭',updated=? WHERE binding=? AND enabled=1",
                            (time.time(), self.binding))
            self.db.execute("UPDATE auto_messages SET state='pending',reason='resuming local work' "
                            "WHERE binding=? AND state IN ('processing','prepared')", (self.binding,))
            self.db.execute("UPDATE auto_messages SET state='cancelled',reason='service restarted' "
                            "WHERE binding=? AND state='pending'", (self.binding,))
            self.db.execute("UPDATE auto_messages SET state='uncertain',reason='reply interrupted' "
                            "WHERE binding=? AND state='sending'", (self.binding,))
            self.db.execute("UPDATE auto_messages SET state='notice_uncertain',reason='notice interrupted' "
                            "WHERE binding=? AND state='notifying'", (self.binding,))

    def _set(self, message_id, state, reason='', **fields):
        values = {'state': state, 'reason': reason[:100], **fields}
        assignments = ','.join(f'{key}=?' for key in values)
        with self.db:
            self.db.execute(f'UPDATE auto_messages SET {assignments} WHERE binding=? AND message_id=?',
                            (*values.values(), self.binding, message_id))

    async def _withdraw_reaction(self, message_id, reaction_id):
        if not reaction_id:
            return
        try:
            await self.adapter.withdraw_reaction(message_id, reaction_id)
        except Exception as error:
            self._failure('reaction_cleanup_failed', type(error).__name__)

    async def process_once(self):
        """Process one queued message with a supplied, bounded read-only query."""
        if not self.active():
            return False
        row = self.db.execute('SELECT message_id,chat_id,conversation,sender_id,sender_name,body,generation '
                              "FROM auto_messages WHERE binding=? AND state='pending' ORDER BY created LIMIT 1",
                              (self.binding,)).fetchone()
        if not row:
            return False
        mid, chat, conversation, sender, name, body, generation = row
        if generation != self.control()[1] or chat in self.watched_chats():
            self._set(mid, 'cancelled', 'generation changed or group monitored')
            return True
        self._set(mid, 'processing')
        context = self.context(conversation, sender)
        try:
            decision = await self.query.prepare(body, context)
        except Exception as error:
            self._failure('decision_failed', type(error).__name__)
            self._set(mid, 'ignored', 'decision failed')
            return True
        source = None if decision['action'] == 'ignore' else decision['service']
        if source is None:
            self._set(mid, 'ignored', 'outside auto services')
            return True
        if any(pattern.search(body) for pattern in (SECRET, PERSONAL)):
            self._set(mid, 'ignored', 'sensitive request')
            return True
        if not self.active() or generation != self.control()[1]:
            self._set(mid, 'cancelled', 'auto disabled during decision')
            return True
        reaction_id = ''
        try:
            reaction = await self.adapter.react_thinking(mid)
            if isinstance(reaction, str) and reaction:
                reaction_id = reaction
            elif reaction is not True:
                raise AdapterUnavailable('表情未确认成功')
        except Exception as error:
            self._failure('reaction_failed', type(error).__name__)
        try:
            result = await self.query.execute(body, context, decision)
            if not isinstance(result, dict):
                raise ValueError('查询结果无效')
            answer = result.get('answer')
            if isinstance(answer, str):
                from assistant.auto_sources import scrub
                answer = scrub(answer)
        except Exception as error:
            self._failure('query_failed', type(error).__name__)
            await self._withdraw_reaction(mid, reaction_id)
            self._set(mid, 'ignored', type(error).__name__)
            return True
        if result.get('needs_owner') is True:
            if not self.active() or generation != self.control()[1]:
                self._set(mid, 'cancelled', 'auto disabled')
                return True
            self._set(mid, 'notifying')
            from assistant.auto_sources import scrub
            excerpt = scrub(body).replace('\n', ' ')[:240]
            detail = result.get('authorization_detail')
            detail_line = ('\n权限情况：' + scrub(detail)[:180]) if isinstance(detail, str) and detail else ''
            notice = (f'/auto 需要你的授权或确认：{source}；来自 {name}；原消息 {mid}。\n'
                      f'请求：{excerpt}{detail_line}\n请在本私聊中说明是否同意及范围。')
            try:
                notice_id = await self.adapter.notify_owner(notice)
                if not isinstance(notice_id, str) or not notice_id:
                    raise ValueError('通知回执缺失')
            except Exception as error:
                self._failure('notice_failed', type(error).__name__)
                self._set(mid, 'notice_uncertain', type(error).__name__)
                return True
            self._set(mid, 'notified', notice_id=notice_id)
            return True
        if (result.get('handled') is False or not isinstance(answer, str) or
                not answer.replace('[已隐藏]', '').strip() or
                not safe_result(answer, public=result.get('public') is True,
                                verified_source=result.get('verified_source') is True)):
            await self._withdraw_reaction(mid, reaction_id)
            self._set(mid, 'ignored', 'no safe answer')
            return True
        reply = sign_message(salutation(name) + '，' + answer.strip(), self.cfg)
        self._set(mid, 'prepared')
        if not self.active() or generation != self.control()[1] or chat in self.watched_chats():
            self._set(mid, 'cancelled', 'disabled or group monitored')
            return True
        self._set(mid, 'sending')
        try:
            reply_id = await self.adapter.reply_as_owner(
                {'message_id': mid, 'chat_id': chat, 'sender_id': sender,
                 'reply_in_thread': conversation.startswith('group:')}, reply)
            if not isinstance(reply_id, str) or not reply_id:
                raise ValueError('发送回执缺失')
        except Exception as error:
            self._set(mid, 'uncertain', type(error).__name__)
            await self.stop_for_failure('用户身份发送失败', error)
            return True
        self._set(mid, 'replied', reply_id=reply_id)
        self.remember(conversation, mid, 'user', body)
        self.remember(conversation, reply_id, 'assistant', reply)
        return True

    async def scheduler(self):
        """Poll and drain only while the owner has enabled /auto."""
        import asyncio
        while True:
            try:
                if self.active():
                    await self.poll_once()
                    for _ in range(5):
                        if not self.active() or not await self.process_once():
                            break
            except Exception as error:
                self._failure('scheduler_failed', type(error).__name__)
                await self.stop_for_failure('自动处理失败', error)
            await asyncio.sleep(2 if self.active() else 5)
